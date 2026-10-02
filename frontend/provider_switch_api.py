"""Authenticated transport for the application-owned, transient review service.

No correspondence, classification or application policy lives in this module.
"""

from asyncio import run
from functools import wraps
from json import loads
from re import fullmatch

from flask import current_app, request

from backend.base.custom_exceptions import MetadataSourceRateLimitReached
from backend.base.logging import LOGGER
from backend.base.provider_switch import ProviderReference
from backend.base.switch_review import SwitchReviewError
from backend.implementations.metadata.errors import MetadataProviderError
from backend.implementations.metadata.registry import PROVIDERS
from backend.internals.db import get_db
from backend.internals.provider_authority import AuthorityToken


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise SwitchReviewError('invalid_request')
    return value


def _body():
    if not request.is_json:
        raise SwitchReviewError('invalid_request')
    raw = request.stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise SwitchReviewError('request_size_limit')
    try:
        return loads(raw)
    except (ValueError, UnicodeError):
        raise SwitchReviewError('invalid_request') from None


def _integer(value, minimum=1, maximum=10000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise SwitchReviewError('invalid_request')
    return value


def _reference(provider, identity):
    if not isinstance(provider, str) or provider not in PROVIDERS:
        raise SwitchReviewError('invalid_provider')
    try:
        return ProviderReference(provider, identity)
    except ValueError:
        raise SwitchReviewError('invalid_provider_identity') from None


def _session_id(value):
    if not isinstance(value, str) or not fullmatch(r'[A-Za-z0-9_-]{43}', value):
        raise SwitchReviewError('invalid_session')
    return value


def _review(service, session):
    preview = session.preview.view()
    current = session.local.view()['volume']
    target = preview['target_volume']
    # Display projection of the backend's field ownership contract, not a new
    # metadata reconciliation policy. Non-owned observations are not changes.
    preview['volume_fields'] = []
    for field in ('title', 'year', 'publisher', 'description', 'site_url', 'volume_number', 'alt_title'):
        proposed = (target['aliases'] or [None])[0] if field == 'alt_title' else target[field]
        preview['volume_fields'].append(dict(field=field, current=current[field], target=proposed,
            owned=field in preview['application_fields']['volume'], changed=field in preview['volume_deltas']))
    # The target choices are all admitted identities, not client metadata.
    return dict(session_id=session.id, revision=session.revision,
        created_at=session.created_at, expires_in=max(0, session.expires_at - service.clock()),
        source_authority=dict(volume_id=session.volume_id, **preview['source'],
                              generation=preview['source_generation']),
        overrides=[dict(local_issue_id=local, target_provider_id=target) for local, target in session.overrides],
        target_issues=session.target.data.view()['issues'], preview=preview)


def _outcome(reason):
    # Only controlled codes leave the process, never arbitrary exception text.
    if reason == 'review_expired_or_unavailable':
        return 'review_unavailable', 410
    if reason == 'request_size_limit':
        return reason, 413
    if reason in ('stale_review_revision', 'switch_retry_identity_mismatch'):
        return 'invalid_revision', 409
    if reason == 'stale_mapping_digest':
        return 'mapping_digest_mismatch', 409
    if reason in ('stale_metadata_authority', 'source_changed_during_fetch', 'switch_source_mismatch'):
        return 'authority_changed', 409
    if reason == 'stale_local_state':
        return 'stale_review', 409
    if reason == 'switch_review_blocked':
        return 'review_blocked', 409
    if reason in ('review_size_limit', 'session_size_limit', 'target_issue_limit', 'review_acquisition_busy',
                  'local_review_row_limit', 'identity_review_limit', 'claim_review_limit', 'coverage_review_limit'):
        return 'review_limit', 429
    if reason == 'unavailable_or_same_provider_volume':
        return 'same_provider_or_missing_volume', 409
    if reason == 'switch_receipt_unavailable':
        return 'receipt_unavailable', 404
    if reason == 'complete_review_capability_unavailable':
        return 'provider_unavailable', 503
    if reason in ('incomplete_target', 'duplicate_or_incomplete_target', 'wrong_issue_parent',
                  'target_identity_mismatch', 'snapshot_owner_mismatch', 'invalid_fact_owner'):
        return 'target_incomplete', 409
    if reason.startswith('invalid_') or reason == 'explicit_switch_confirmation_required':
        return 'invalid_request', 400
    return 'review_conflict', 409


def register(api, auth, error_handler, return_api):
    def transport(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            try:
                if request.content_length and request.content_length > 2 * 1024 * 1024:
                    return return_api({'reason': 'request_size_limit'}, 'ProviderSwitchFailure', 413)
                result = function(*args, **kwargs)
                return return_api(result)
            except MetadataProviderError as error:
                reason, code = {
                    'credentials': ('provider_auth_required', 503),
                    'forbidden': ('provider_auth_required', 503),
                    'disabled': ('provider_disabled', 503),
                    'rate_limited': ('provider_rate_limited', 429),
                    'deferred': ('provider_rate_limited', 429),
                    'budget': ('provider_rate_limited', 429),
                    'not_found': ('target_not_found', 404),
                    'incomplete': ('target_incomplete', 409),
                }.get(error.reason, ('provider_unavailable', 502))
                return return_api({'reason': reason}, 'ProviderSwitchFailure', code)
            except MetadataSourceRateLimitReached:
                return return_api({'reason': 'provider_rate_limited'}, 'ProviderSwitchFailure', 429)
            except SwitchReviewError as error:
                reason, code = _outcome(str(error))
                return return_api({'reason': reason}, 'ProviderSwitchFailure', code)
            except ValueError:
                # Exact correspondence/admission rejects malformed provider data.
                return return_api({'reason': 'target_or_mapping_invalid'}, 'ProviderSwitchFailure', 409)
            except Exception:
                # No exception repr/traceback: transports can include secret URLs.
                LOGGER.error('Provider switch transport failed unexpectedly')
                return return_api({'reason': 'internal_error'}, 'ProviderSwitchFailure', 500)
        return error_handler(auth(wrapper))

    def service():
        return current_app.extensions['provider_switch_reviews']

    @api.route('/provider-switch/reviews', methods=['POST'])
    @transport
    def switch_create():
        body = _object(_body(), ('volume_id', 'provider', 'provider_id'))
        volume = _integer(body['volume_id'], maximum=2**63 - 1)
        target = _reference(body['provider'], body['provider_id'])
        owner = service()
        return _review(owner, run(owner.create(get_db(), volume, target.provider, target.provider_id)))

    @api.route('/provider-switch/reviews/<identifier>', methods=['GET', 'PUT', 'DELETE'])
    @transport
    def switch_review(identifier):
        _session_id(identifier)
        owner = service()
        if request.method == 'DELETE':
            owner.delete(identifier)
            return {'cancelled': True}
        if request.method == 'GET':
            return _review(owner, owner.get(get_db(), identifier))
        body = _object(_body(), ('revision', 'mappings'))
        _integer(body['revision'], maximum=2**31 - 1)
        mappings = body['mappings']
        if not isinstance(mappings, list) or len(mappings) > 10000:
            raise SwitchReviewError('invalid_manual_mapping')
        overrides = {}
        for mapping in mappings:
            _object(mapping, ('local_issue_id', 'target_provider_id'))
            local = _integer(mapping['local_issue_id'], maximum=2**63 - 1)
            if local in overrides or not isinstance(mapping['target_provider_id'], str):
                raise SwitchReviewError('invalid_manual_mapping')
            overrides[local] = mapping['target_provider_id']
        return _review(owner, owner.revise(get_db(), identifier, body['revision'], overrides))

    @api.route('/provider-switch/reviews/<identifier>/apply', methods=['POST'])
    @transport
    def switch_apply(identifier):
        _session_id(identifier)
        body = _object(_body(), ('revision', 'mapping_digest', 'confirmed', 'source_authority'))
        token = _object(body['source_authority'], ('volume_id', 'provider', 'provider_id', 'generation'))
        _integer(token['volume_id'], maximum=2**63 - 1)
        _integer(token['generation'], minimum=0, maximum=2**63 - 1)
        _reference(token['provider'], token['provider_id'])
        # Do NOT retrieve the transient review here: durable retry comes first.
        return service().apply(get_db(), identifier, body['revision'], body['mapping_digest'],
            confirmed=body['confirmed'], expected_authority=AuthorityToken(**token))

    def page(key, default, minimum, maximum):
        value = request.args.get(key, str(default))
        if not value.isascii() or not value.isdecimal():
            raise SwitchReviewError('invalid_page')
        return _integer(int(value), minimum, maximum)

    @api.route('/volumes/<int:volume_id>/provider-switch/history', methods=['GET'])
    @transport
    def switch_history(volume_id):
        _integer(volume_id, maximum=2**63 - 1)
        before = page('before_generation', 1, 1, 2**63 - 1) if 'before_generation' in request.args else None
        return service().history(get_db(), volume_id, before_generation=before, limit=page('limit', 25, 1, 100))

    @api.route('/provider-switch/receipts/<identifier>', methods=['GET'])
    @transport
    def switch_receipt(identifier):
        if not fullmatch(r'[a-f0-9]{32}', identifier) or request.args.get('detail', '0') not in ('0', '1'):
            raise SwitchReviewError('invalid_receipt')
        return service().receipt(get_db(), identifier, detail=request.args.get('detail') == '1',
            offset=page('offset', 0, 0, 20000), limit=page('limit', 100, 1, 500))
