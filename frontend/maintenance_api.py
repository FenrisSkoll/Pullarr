"""Authenticated, bounded transport; domain services own all review policy.

Action routes delegate to trusted workers and existing domain confirmations.
Task completion is not a durable library mutation receipt.
"""

from dataclasses import asdict
from functools import wraps
from json import dumps, loads
from re import fullmatch

from flask import current_app, request

from backend.base.bulk_folder import FolderReviewError
from backend.base.bulk_rename import RenameReviewError
from backend.base.duplicate_review import (DuplicateAction, DuplicateChoice,
                                           DuplicateReviewError)
from backend.base.library_health import (HealthLevel, HealthScope,
                                         HealthSeverity, InspectionStatus)
from backend.base.logging import LOGGER
from backend.base.maintenance_history import (DOMAINS, HistoryCursor,
                                              HistoryError, HistoryFilter)
from backend.base.maintenance_review import (Action, Capability, Edit,
                                             FindingFilter, ReviewError)
from backend.base.metadata_repair import RepairError
from backend.base.switch_review import SwitchReviewError
from backend.features.comicinfo_repair import FIELDS
from backend.features.maintenance_actions import action_error
from backend.features.maintenance_runtime import review_failure

MAX_BODY = 512 * 1024
MAX_RESPONSE = 2 * 1024 * 1024


class TransportError(ValueError):
    def __init__(self, reason='invalid_request', status=400):
        self.reason, self.status = reason, status


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise TransportError()
    return value


def _body(keys):
    if not request.is_json:
        raise TransportError()
    raw = request.stream.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise TransportError('request_too_large', 413)
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise TransportError()
            result[key] = value
        return result
    try:
        return _object(loads(raw, object_pairs_hook=unique_object), keys)
    except (ValueError, UnicodeError):
        raise TransportError() from None


def _integer(value, minimum=0, maximum=1000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise TransportError()
    return value


def _boolean(value):
    if type(value) is not bool:
        raise TransportError()
    return value


def _id(value, length=32):
    if not isinstance(value, str) or not fullmatch('[0-9a-f]{' + str(length) + '}', value):
        raise TransportError()
    return value


def _history_id(value):
    if not isinstance(value, str) or not fullmatch(r'[A-Za-z0-9_:.-]{1,128}', value):
        raise TransportError()
    return value


def _review_id(value):
    if not isinstance(value, str) or not fullmatch(r'[A-Za-z0-9_-]{32}', value):
        raise TransportError()
    return value


def _selection(value, maximum=250, minimum=1):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise TransportError()
    result = [_id(item, 64) for item in value]
    if len(set(result)) != len(result):
        raise TransportError()
    return result


def _origin(value):
    if not isinstance(value, list) or len(value) != 3:
        raise TransportError()
    return [_id(value[0]), _integer(value[1]), _id(value[2], 64)]


def _query(keys):
    if len(request.query_string) > 8192:
        raise TransportError('request_too_large', 413)
    if set(request.args) - set(keys) - {'api_key'}:
        raise TransportError()
    if any(len(request.args.getlist(key)) != 1 for key in request.args):
        raise TransportError()
    return {k: v for k, v in request.args.items() if k != 'api_key'}


def _number(query, key, default=None, minimum=0, maximum=1000):
    if key not in query:
        return default
    value = query[key]
    if not fullmatch(r'0|[1-9][0-9]{0,18}', value):
        raise TransportError()
    return _integer(int(value), minimum, maximum)


def _page(query, maximum=2000):
    return dict(offset=_number(query, 'offset', 0, maximum=maximum),
                limit=_number(query, 'limit', 50, minimum=1, maximum=100))


def _filters(value):
    keys = set(FindingFilter.__dataclass_fields__)
    if not isinstance(value, dict) or set(value) - keys:
        raise TransportError()
    return FindingFilter(**value)


def _finding(value):
    # No unrestricted evidence JSON, raw ComicInfo or provider responses.
    return {key: value[key] for key in (
        'id', 'category', 'code', 'severity', 'inspection', 'level', 'volume_id',
        'issue_ids', 'file_id', 'path', 'explanation', 'actionable_later', 'repair_known')}


def register(api, auth, error_handler, return_api):
    def transport(function):
        @wraps(function)
        def wrapper(*args, **kwargs):
            try:
                if request.content_length and request.content_length > MAX_BODY:
                    raise TransportError('request_too_large', 413)
                result = function(*args, **kwargs)
                if len(dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_RESPONSE:
                    raise TransportError('bounded', 409)
                return return_api(result)
            except TransportError as error:
                return return_api({'reason': error.reason}, 'MaintenanceFailure', error.status)
            except ReviewError as error:
                reason, status = review_failure(error)
                return return_api({'reason': reason}, 'MaintenanceFailure', status)
            except (FolderReviewError, RenameReviewError, RepairError, SwitchReviewError, DuplicateReviewError) as error:
                reason = action_error(error)
                return return_api({'reason': reason}, 'MaintenanceFailure', 410 if reason == 'review_expired' else 409)
            except HistoryError as error:
                reason = str(error)
                unavailable = reason in ('history_entry_unavailable', 'history_batch_unavailable')
                bounded = reason in ('history_projection_too_large', 'history_batch_too_large')
                return return_api({'reason': 'history_unavailable' if unavailable else
                    'bounded' if bounded else 'invalid_request'}, 'MaintenanceFailure',
                    404 if unavailable else 409 if bounded else 400)
            except (TypeError, ValueError):
                return return_api({'reason': 'invalid_request'}, 'MaintenanceFailure', 400)
            except Exception:
                LOGGER.error('Maintenance transport failed unexpectedly')
                return return_api({'reason': 'internal_error'}, 'MaintenanceFailure', 500)
        return error_handler(auth(wrapper))

    def owner():
        return current_app.extensions['maintenance']

    @api.route('/maintenance/volumes', methods=['GET'])
    @transport
    def maintenance_volume_picker():
        query = _query(('q', 'after', 'limit'))
        return owner().volume_page(query.get('q', ''),
            _number(query, 'after', 0, maximum=2**63 - 1),
            _number(query, 'limit', 50, minimum=1, maximum=100))

    @api.route('/maintenance/scans', methods=['POST'])
    @transport
    def maintenance_scan_create():
        _query(())
        body = _body(('scope', 'level'))
        scope = _object(body['scope'], ('kind', 'ids'))
        if not isinstance(scope['ids'], list) or len(scope['ids']) > 1000:
            raise TransportError()
        ids = tuple(_integer(i, 1, 2**63 - 1) for i in scope['ids'])
        identifier = owner().reviews.request_scan(HealthScope(scope['kind'], ids), HealthLevel(body['level']))
        return owner().reviews.scan_status(identifier)

    @api.route('/maintenance/scans/<identifier>', methods=['GET'])
    @transport
    def maintenance_scan_status(identifier):
        _query(())
        return owner().reviews.scan_status(_id(identifier))

    @api.route('/maintenance/scans/<identifier>/cancel', methods=['POST'])
    @transport
    def maintenance_scan_cancel(identifier):
        _query(())
        _body(())
        owner().reviews.cancel_scan(_id(identifier))
        return dict(cancellation_requested=True, immediate_io_interrupt=False)

    @api.route('/maintenance/scans/<identifier>/findings', methods=['GET'])
    @transport
    def maintenance_findings(identifier):
        query = _query(('offset', 'limit', 'category', 'severity', 'inspection', 'volume_id', 'actionable'))
        filters = {k: query[k] for k in ('category',) if k in query}
        if 'severity' in query:
            filters['severity'] = HealthSeverity(query['severity'])
        if 'inspection' in query:
            filters['inspection'] = InspectionStatus(query['inspection'])
        if 'volume_id' in query:
            filters['volume_id'] = _number(query, 'volume_id', minimum=1, maximum=2**63 - 1)
        if 'actionable' in query:
            if query['actionable'] not in ('true', 'false'):
                raise TransportError()
            filters['actionable'] = query['actionable'] == 'true'
        page = owner().reviews.findings(_id(identifier), **_page(query, 10000), **filters)
        page['findings'] = [_finding(f) for f in page['findings']]
        return page

    @api.route('/maintenance/worklists', methods=['POST'])
    @transport
    def maintenance_worklist_create():
        _query(())
        body = _body(('scan_id',))
        return owner().reviews.create(_id(body['scan_id'])).summary()

    @api.route('/maintenance/worklists/<identifier>', methods=['GET', 'DELETE'])
    @transport
    def maintenance_worklist(identifier):
        _query(())
        _id(identifier)
        if request.method == 'DELETE':
            body = _body(('revision',))
            owner().reviews.delete(identifier, _integer(body['revision']))
            return dict(cancelled=True)
        review = owner().reviews.get(identifier)
        result = review.summary()
        result['rename_selection'] = [item.finding.id for item in review.items
            if item.selected and not item.excluded and item.action == Action.RENAME]
        result['folder_selection'] = [item.finding.id for item in review.items
            if item.selected and not item.excluded and item.action == Action.FOLDER]
        result['specialized'] = dict(metadata=[], comicinfo=[], duplicate=[])
        grouped = {}
        for item in review.items:
            if not item.selected or item.excluded:
                continue
            if item.action == Action.METADATA:
                grouped.setdefault(item.finding.volume_id, []).append(item.finding.id)
            elif item.action == Action.COMICINFO and item.capability == Capability.PREVIEW and not item.blockers:
                result['specialized']['comicinfo'].append(dict(finding_id=item.finding.id, file_id=item.finding.file_id))
            elif item.action == Action.DUPLICATE:
                result['specialized']['duplicate'].append(item.finding.id)
        result['specialized']['metadata'] = [dict(volume_id=key, selected=value) for key, value in grouped.items()]
        return result

    @api.route('/maintenance/worklists/<identifier>/items', methods=['GET'])
    @transport
    def maintenance_worklist_items(identifier):
        query = _query(('offset', 'limit', *FindingFilter.__dataclass_fields__))
        filters = {k: v for k, v in query.items() if k not in ('offset', 'limit')}
        for key in ('selected', 'excluded'):
            if key in filters:
                if filters[key] not in ('true', 'false'):
                    raise TransportError()
                filters[key] = filters[key] == 'true'
        if 'volume_id' in filters:
            filters['volume_id'] = _number(query, 'volume_id', minimum=1, maximum=2**63 - 1)
        page = owner().reviews.get(_id(identifier)).page(**_page(query), filters=_filters(filters))
        # Worklist previews can contain internal organizer evidence. The
        # action-specific transport must project exact effects separately.
        page['items'] = [dict(finding=_finding(i['finding']), selected=i['selected'],
            excluded=i['excluded'], action=i['action'], capability=i['capability'],
            blockers=i['blockers'], recovery=i['recovery'], apply_available=False) for i in page['items']]
        return page

    @api.route('/maintenance/worklists/<identifier>/revise', methods=['POST'])
    @transport
    def maintenance_worklist_revise(identifier):
        _query(())
        body = _body(('revision', 'edits'))
        if not isinstance(body['edits'], list) or len(body['edits']) > 2000:
            raise TransportError()
        edits = []
        for value in body['edits']:
            item = _object(value, ('finding_id', 'selected', 'excluded', 'action'))
            edits.append(Edit(_id(item['finding_id'], 64), _boolean(item['selected']),
                              _boolean(item['excluded']), Action(item['action'])))
        return owner().submit_worklist('revise', _id(identifier), _integer(body['revision']), tuple(edits))

    @api.route('/maintenance/worklists/<identifier>/select-filtered', methods=['POST'])
    @transport
    def maintenance_worklist_filtered(identifier):
        _query(())
        body = _body(('revision', 'report_id', 'snapshot_digest', 'filters', 'selected'))
        return owner().submit_worklist('select_filtered', _id(identifier), _integer(body['revision']),
            _id(body['report_id']), _id(body['snapshot_digest'], 64), _filters(body['filters']),
            _boolean(body['selected']))

    @api.route('/maintenance/worklists/<identifier>/revalidate', methods=['POST'])
    @transport
    def maintenance_worklist_revalidate(identifier):
        _query(())
        body = _body(('revision',))
        return owner().submit_worklist('revalidate', _id(identifier), _integer(body['revision']))

    @api.route('/maintenance/review-tasks/<identifier>', methods=['GET'])
    @transport
    def maintenance_review_task(identifier):
        _query(())
        return owner().delivery(_id(identifier))

    @api.route('/maintenance/history', methods=['GET'])
    @transport
    def maintenance_history_page():
        query = _query((*HistoryFilter.__dataclass_fields__, 'before', 'limit'))
        values = {k: v for k, v in query.items() if k not in ('before', 'limit')}
        for key in ('volume_id', 'file_id'):
            if key in values:
                values[key] = _number(query, key, minimum=1, maximum=2**63 - 1)
        filters = HistoryFilter(**values)
        before = None
        if 'before' in query:
            cursor = _object(loads(query['before']), ('time', 'domain', 'identifier', 'filters', 'version'))
            bound = _object(cursor['filters'], HistoryFilter.__dataclass_fields__)
            before = HistoryCursor(cursor['time'], cursor['domain'], cursor['identifier'],
                                   HistoryFilter(**bound), cursor['version'])
        page = owner().history.page(filters, before=before,
            limit=_number(query, 'limit', 50, minimum=1, maximum=100))
        if page['next_cursor'] is not None:
            page['next_cursor'] = asdict(page['next_cursor'])
        return page

    @api.route('/maintenance/history/<domain>/<identifier>', methods=['GET'])
    @transport
    def maintenance_history_detail(domain, identifier):
        query = _query(('offset', 'limit'))
        if domain not in DOMAINS:
            raise TransportError()
        identity = _history_id(identifier)
        if domain in ('content_claim', 'content_coverage', 'intake'):
            _number({'id': identity}, 'id', minimum=1, maximum=2**63 - 1)
        paging = _page(query, 40000)
        result = owner().history.detail(domain, identity, **paging)
        detail = result['detail']
        offset, limit = paging['offset'], paging['limit']
        if domain == 'content_claim':
            evidence = detail['evidence']
            detail['evidence'] = evidence[offset:offset + limit]
            more = offset + limit < len(evidence)
        elif domain == 'metadata_repair':
            more = offset + limit < detail['receipt']['field_count']
        else:
            more = any(isinstance(value, list) and len(value) == limit for value in detail.values())
        result['page']['has_next'] = more and offset + limit <= 40000
        return result

    @api.route('/maintenance/batches/<identifier>', methods=['GET'])
    @transport
    def maintenance_history_batch(identifier):
        query = _query(('offset', 'limit'))
        if not fullmatch(r'[A-Za-z0-9_:./-]{1,512}', identifier):
            raise TransportError()
        return owner().history.batch(identifier, **_page(query, 2000))

    @api.route('/maintenance/rename/reviews', methods=['POST'])
    @transport
    def maintenance_rename_create():
        _query(())
        body = _body(('worklist_id', 'revision', 'digest', 'selected'))
        payload = dict(worklist_id=_id(body['worklist_id']), revision=_integer(body['revision']),
                       digest=_id(body['digest'], 64), selected=_selection(body['selected']))
        return owner().actions.submit('rename_create', payload)

    @api.route('/maintenance/rename/reviews/<identifier>', methods=['GET'])
    @transport
    def maintenance_rename_review(identifier):
        query = _query(('offset', 'limit'))
        return owner().actions.rename_page(_review_id(identifier), **_page(query, 250))

    @api.route('/maintenance/rename/reviews/<identifier>/selection', methods=['POST'])
    @transport
    def maintenance_rename_selection(identifier):
        _query(())
        body = _body(('revision', 'selected'))
        return owner().actions.submit('rename_revise', dict(id=_review_id(identifier),
            revision=_integer(body['revision']), selected=_selection(body['selected'])))

    @api.route('/maintenance/rename/reviews/<identifier>/apply', methods=['POST'])
    @transport
    def maintenance_rename_apply(identifier):
        _query(())
        body = _body(('revision', 'digest', 'origin', 'selected', 'confirmed'))
        if body['confirmed'] is not True:
            raise TransportError()
        return owner().actions.submit('rename_apply', dict(id=_review_id(identifier),
            revision=_integer(body['revision']), digest=_id(body['digest'], 64),
            origin=_origin(body['origin']), selected=_selection(body['selected'])))

    @api.route('/maintenance/action-tasks/<identifier>', methods=['GET'])
    @transport
    def maintenance_action_status(identifier):
        _query(())
        return owner().actions.status(_id(identifier))

    def repair_family(family):
        if family not in ('metadata', 'comicinfo'):
            raise TransportError('unsupported', 400)
        return family

    @api.route('/maintenance/repair/<family>/reviews', methods=['POST'])
    @transport
    def maintenance_repair_create(family):
        _query(())
        repair_family(family)
        body = _body(('worklist_id', 'revision', 'digest', 'selected' if family == 'metadata' else 'finding_id'))
        payload = dict(worklist_id=_id(body['worklist_id']), revision=_integer(body['revision']), digest=_id(body['digest'], 64))
        payload.update(dict(selected=_selection(body['selected'], 2000)) if family == 'metadata'
                       else dict(finding_id=_id(body['finding_id'], 64)))
        return owner().actions.submit(family + '_create', payload)

    @api.route('/maintenance/repair/<family>/reviews/<identifier>', methods=['GET'])
    @transport
    def maintenance_repair_page(family, identifier):
        query = _query(('offset', 'limit'))
        return owner().actions.specialized.repair_page(repair_family(family), _review_id(identifier), **_page(query, 40000))

    @api.route('/maintenance/repair/<family>/reviews/<identifier>/selection', methods=['POST'])
    @transport
    def maintenance_repair_selection(family, identifier):
        _query(())
        repair_family(family)
        body = _body(('revision', 'edits' if family == 'metadata' else 'selected'))
        payload = dict(id=_review_id(identifier), revision=_integer(body['revision']))
        if family == 'metadata':
            if not isinstance(body['edits'], list) or not 1 <= len(body['edits']) <= 100:
                raise TransportError()
            edits = []
            for value in body['edits']:
                edit = _object(value, ('key', 'selected'))
                if not isinstance(edit['key'], str) or not fullmatch(r'(volume|issue):[1-9][0-9]{0,18}:[a-z_]{1,40}', edit['key']):
                    raise TransportError()
                edits.append(dict(key=edit['key'], selected=_boolean(edit['selected'])))
            if len({e['key'] for e in edits}) != len(edits):
                raise TransportError()
            payload['edits'] = edits
        else:
            selected = body['selected']
            if (not isinstance(selected, list) or len(selected) > len(FIELDS)
                    or any(type(k) is not str or k not in FIELDS for k in selected) or len(set(selected)) != len(selected)):
                raise TransportError()
            payload['selected'] = selected
        return owner().actions.submit(family + '_revise', payload)

    @api.route('/maintenance/repair/<family>/reviews/<identifier>/apply', methods=['POST'])
    @transport
    def maintenance_repair_apply(family, identifier):
        _query(())
        repair_family(family)
        body = _body(('revision', 'digest', 'confirmed'))
        if body['confirmed'] is not True:
            raise TransportError()
        return owner().actions.submit(family + '_apply', dict(id=_review_id(identifier),
            revision=_integer(body['revision']), digest=_id(body['digest'], 64)))

    @api.route('/maintenance/repair/<family>/reviews/<identifier>/result', methods=['GET'])
    @transport
    def maintenance_repair_result(family, identifier):
        query = _query(('revision', 'digest'))
        if set(query) != {'revision', 'digest'}:
            raise TransportError()
        return owner().actions.specialized.repair_result(repair_family(family), _review_id(identifier),
            _number(query, 'revision'), _id(query['digest'], 64))

    @api.route('/maintenance/duplicate/reviews', methods=['POST'])
    @transport
    def maintenance_duplicate_create():
        _query(())
        body = _body(('worklist_id', 'revision', 'digest', 'selected'))
        return owner().actions.submit('duplicate_create', dict(worklist_id=_id(body['worklist_id']),
            revision=_integer(body['revision']), digest=_id(body['digest'], 64), selected=_selection(body['selected'], 1000)))

    @api.route('/maintenance/duplicate/reviews/<identifier>', methods=['GET'])
    @transport
    def maintenance_duplicate_page(identifier):
        query = _query(('offset', 'limit', 'group_id'))
        group_id = _id(query['group_id'], 64) if 'group_id' in query else None
        return owner().actions.specialized.duplicate_page(_review_id(identifier), group_id=group_id, **_page(query))

    @api.route('/maintenance/duplicate/reviews/<identifier>/selection', methods=['POST'])
    @transport
    def maintenance_duplicate_selection(identifier):
        _query(())
        body = _body(('revision', 'choices'))
        if not isinstance(body['choices'], list) or not 1 <= len(body['choices']) <= 100:
            raise TransportError()
        choices = []
        for value in body['choices']:
            choice = _object(value, ('group_id', 'action', 'quarantine'))
            if not isinstance(choice['quarantine'], list) or len(choice['quarantine']) > 128:
                raise TransportError()
            choices.append(DuplicateChoice(_id(choice['group_id'], 64), DuplicateAction(choice['action']),
                tuple(_integer(i, 1, 2**63 - 1) for i in choice['quarantine'])))
        return owner().actions.submit('duplicate_revise', dict(id=_review_id(identifier),
            revision=_integer(body['revision']), choices=choices))

    @api.route('/maintenance/duplicate/reviews/<identifier>/prepare', methods=['POST'])
    @transport
    def maintenance_duplicate_prepare(identifier):
        _query(())
        body = _body(('revision',))
        return owner().actions.submit('duplicate_prepare', dict(id=_review_id(identifier), revision=_integer(body['revision'])))

    @api.route('/maintenance/duplicate/reviews/<identifier>/apply', methods=['POST'])
    @transport
    def maintenance_duplicate_apply(identifier):
        _query(())
        body = _body(('revision', 'digest', 'origin', 'selected', 'confirmed'))
        if body['confirmed'] is not True:
            raise TransportError()
        return owner().actions.submit('duplicate_apply', dict(id=_review_id(identifier),
            revision=_integer(body['revision']), digest=_id(body['digest'], 64),
            origin=_origin(body['origin']), selected=_selection(body['selected'], 100)))

    @api.route('/maintenance/folder/reviews', methods=['POST'])
    @transport
    def maintenance_folder_create():
        _query(())
        body = _body(('worklist_id', 'revision', 'digest', 'selected', 'canonical_custom'))
        selected = _selection(body['selected'], 50)
        canonical = _selection(body['canonical_custom'], 50, 0)
        if not set(canonical).issubset(selected):
            raise TransportError()
        return owner().actions.submit('folder_create', dict(worklist_id=_id(body['worklist_id']),
            revision=_integer(body['revision']), digest=_id(body['digest'], 64),
            selected=selected, canonical_custom=canonical))

    @api.route('/maintenance/folder/reviews/<identifier>', methods=['GET'])
    @transport
    def maintenance_folder_review(identifier):
        query = _query(('offset', 'limit'))
        return owner().actions.folder_page(_review_id(identifier), **_page(query, 50))

    @api.route('/maintenance/folder/reviews/<identifier>/selection', methods=['POST'])
    @transport
    def maintenance_folder_selection(identifier):
        _query(())
        body = _body(('revision', 'selected'))
        return owner().actions.submit('folder_revise', dict(id=_review_id(identifier),
            revision=_integer(body['revision']), selected=_selection(body['selected'], 50)))

    @api.route('/maintenance/folder/reviews/<identifier>/apply', methods=['POST'])
    @transport
    def maintenance_folder_apply(identifier):
        _query(())
        body = _body(('revision', 'digest', 'origin', 'selected', 'confirmed'))
        if body['confirmed'] is not True:
            raise TransportError()
        return owner().actions.submit('folder_apply', dict(id=_review_id(identifier),
            revision=_integer(body['revision']), digest=_id(body['digest'], 64),
            origin=_origin(body['origin']), selected=_selection(body['selected'], 50)))

    @api.route('/maintenance/history/<domain>/<identifier>/recovery-preview', methods=['POST'])
    @transport
    def maintenance_recovery_preview(domain, identifier):
        _query(())
        _body(())
        if domain not in DOMAINS:
            raise TransportError()
        return owner().actions.submit('recovery_preview', dict(domain=domain, id=_history_id(identifier)))

    @api.route('/maintenance/history/<domain>/<identifier>/inverse-preview', methods=['POST'])
    @transport
    def maintenance_inverse_preview(domain, identifier):
        _query(())
        _body(())
        if domain not in DOMAINS:
            raise TransportError()
        return owner().actions.submit('inverse_preview', dict(domain=domain, id=_history_id(identifier)))

    def confirmed_history(identifier, operation):
        _query(())
        body = _body(('digest', 'confirmed'))
        if body['confirmed'] is not True:
            raise TransportError()
        return owner().actions.submit(operation, dict(id=_history_id(identifier), digest=_id(body['digest'], 64)))

    @api.route('/maintenance/history/organization/<identifier>/recover', methods=['POST'])
    @transport
    def maintenance_recover(identifier):
        return confirmed_history(identifier, 'recovery_apply')

    @api.route('/maintenance/history/organization/<identifier>/inverse', methods=['POST'])
    @transport
    def maintenance_inverse(identifier):
        return confirmed_history(identifier, 'inverse_apply')
