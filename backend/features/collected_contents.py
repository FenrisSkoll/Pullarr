"""Authenticated API action contract. Inputs are identities, never paths/ranges."""

from backend.base.content_claims import (ClaimKind, ContentConflict,
                                         PublicationRef)
from backend.internals import content_claims as service


def identifier(value):
    if not isinstance(value, str) or not value or len(value) > 128 or any(ord(c) < 32 for c in value):
        raise ContentConflict('Expected bounded exact identity')
    return value


def positive_id(value):
    if type(value) is not int or value <= 0 or value > 9223372036854775807:
        raise ContentConflict('Expected positive local ID')
    return value


def action(cursor, target_id, operation, body):
    if not isinstance(body, dict):
        raise ContentConflict('Expected explicit action object')
    if operation in ('claim-preview', 'claim-confirm'):
        allowed = {'source_local_id', 'source_provider', 'source_provider_id', 'kind', 'manual', 'preview_token'}
        if set(body) - allowed or type(body.get('manual', False)) is not bool:
            raise ContentConflict('Unexpected claim fields')
        if 'source_local_id' in body:
            if 'source_provider' in body or 'source_provider_id' in body:
                raise ContentConflict('Select exactly one identity form')
            source = service.local_ref(cursor, positive_id(body['source_local_id']))
        else:
            source = PublicationRef(identifier(body.get('source_provider')), identifier(body.get('source_provider_id')))
        try:
            kind = ClaimKind(body.get('kind'))
        except (ValueError, TypeError):
            raise ContentConflict('Explicit partial or complete claim kind required') from None
        manual = body.get('manual', False)
        if operation == 'claim-preview':
            return service.claim_preview(cursor, target_id, source, kind, manual=manual)
        claim = service.confirm_claim(cursor, target_id, source, kind,
                                      identifier(body.get('preview_token')), manual=manual)
        return {'claim_id': claim, 'file_coverage_not_automatically_applied': True}
    if operation in ('coverage-preview', 'coverage-apply'):
        if set(body) - {'file_id', 'claim_ids', 'preview_token'}:
            raise ContentConflict('Unexpected coverage fields')
        file_id = positive_id(body.get('file_id'))
        claim_ids = body.get('claim_ids')
        if not isinstance(claim_ids, list) or not 1 <= len(claim_ids) <= service.PAGE_SIZE:
            raise ContentConflict('Select 1–100 exact claim IDs')
        claim_ids = [identifier(value) for value in claim_ids]
        if operation == 'coverage-preview':
            return service.coverage_preview(cursor, target_id, file_id, claim_ids)
        return {'coverage_ids': service.apply_coverage(cursor, target_id, file_id, claim_ids,
                                                       identifier(body.get('preview_token')))}
    raise ContentConflict('Unknown content action')
