"""Strict archive maintenance transport; server owns paths, hashes and plans."""
from functools import wraps
from json import dumps

from flask import current_app, request

from backend.implementations.archive_normalization import ArchiveFailure
from frontend.maintenance_api import (TransportError, _body, _id,
                                      _integer, _number, _query)


def register(api, auth, error_handler, return_api):
    def transport(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                if request.method != 'GET':
                    _query(())
                result = fn(*args, **kwargs)
                if len(dumps(result).encode())>2*1024*1024:
                    raise TransportError('bounded', 409)
                return return_api(result)
            except TransportError as error:
                return return_api(dict(reason=error.reason), 'ArchiveMaintenanceFailure', error.status)
            except ArchiveFailure as error:
                return return_api(dict(reason=str(error)), 'ArchiveMaintenanceFailure', 409)
            except Exception:
                from backend.base.logging import LOGGER
                LOGGER.error('Archive maintenance transport failed unexpectedly')
                return return_api(dict(reason='internal_error'), 'ArchiveMaintenanceFailure', 500)
        return error_handler(auth(wrapped))

    def owner():
        return current_app.extensions['maintenance'].archives

    def selection(value, limit):
        if not isinstance(value, list) or not 1 <= len(value) <= limit:
            raise TransportError()
        values = [_integer(v, 1, 2**63-1) for v in value]
        if len(set(values)) != len(values):
            raise TransportError()
        return values

    @api.route('/maintenance/archives', methods=['GET'])
    @transport
    def archive_files():
        q = _query(('volume_id','issue_id','after','limit'))
        return owner().files(volume_id=_number(q,'volume_id',minimum=1,maximum=2**63-1),
            issue_id=_number(q,'issue_id',minimum=1,maximum=2**63-1),
            after=_number(q,'after',0,maximum=2**63-1), limit=_number(q,'limit',50,minimum=1,maximum=100))

    @api.route('/maintenance/archives/scan', methods=['POST'])
    @transport
    def archive_scan():
        data = _body(('selected',))
        return owner().submit('scan', dict(selected=selection(data['selected'],1000)))

    @api.route('/maintenance/archives/batch-preview', methods=['POST'])
    @transport
    def archive_preview():
        data = _body(('selected',))
        return owner().submit('preview', dict(selected=selection(data['selected'],100)))

    @api.route('/maintenance/archives/tasks/<identifier>', methods=['GET'])
    @transport
    def archive_status(identifier):
        _id(identifier)
        q = _query(('offset','limit','status'))
        status = q.get('status','all')
        if status not in ('all','healthy','convertible','shared','review_required','blocked','completed'):
            raise TransportError()
        return owner().status(identifier,offset=_number(q,'offset',0,maximum=1000),
            limit=_number(q,'limit',50,minimum=1,maximum=100),status=status)

    @api.route('/maintenance/archives/tasks/<identifier>/cancel', methods=['POST'])
    @transport
    def archive_cancel(identifier):
        _id(identifier); _body(())
        return owner().cancel(identifier)

    @api.route('/maintenance/archives/batch-apply', methods=['POST'])
    @transport
    def archive_apply():
        data = _body(('review_id','selected','confirmed'))
        _id(data['review_id'])
        if data['confirmed'] is not True:
            raise TransportError()
        return owner().apply(data['review_id'], selection(data['selected'],100))
