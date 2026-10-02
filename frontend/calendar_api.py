"""Authenticated bounded Calendar transport; read pages perform no provider IO."""

import json
from functools import wraps
from re import fullmatch

from flask import current_app, request

from backend.base.collections import CollectionError
from backend.base.logging import LOGGER
from backend.base.release_calendar import CalendarError
from backend.internals.db import get_db
from backend.internals.release_calendar import CalendarStore
from frontend.collections_api import body, number, query


def register(api, auth, error_handler, return_api):
    def route(fn):
        @wraps(fn)
        def safe(*args, **kwargs):
            try:
                if request.content_length and request.content_length > 65536:
                    raise CalendarError('bounded')
                value = fn(*args, **kwargs)
                if len(json.dumps(value).encode()) > 2 * 1024 * 1024:
                    raise CalendarError('bounded')
                return return_api(value)
            except (CalendarError, CollectionError) as error:
                reason = str(error)
                if reason not in ('invalid_request', 'invalid_date', 'range_bounded', 'bounded', 'not_found', 'sync_active', 'task_unavailable'):
                    reason = 'invalid_request'
                return return_api(dict(reason=reason), 'CalendarFailure', 404 if reason == 'not_found' else 409 if reason in ('bounded', 'sync_active') else 400)
            except (ValueError, TypeError, UnicodeError):
                return return_api(dict(reason='invalid_request'), 'CalendarFailure', 400)
            except Exception:
                LOGGER.error('Calendar transport failed unexpectedly')
                return return_api(dict(reason='internal_error'), 'CalendarFailure', 500)
        return error_handler(auth(safe))

    @api.route('/calendar', methods=['GET'])
    @route
    def calendar_page():
        q = query(('from', 'to', 'unknown', 'offset', 'limit', 'scope', 'collection', 'volume', 'provider', 'precision', 'kind', 'ownership'))
        if q.get('unknown', 'false') not in ('true', 'false'):
            raise CalendarError('invalid_request')
        result = CalendarStore(get_db()).page(start=q.get('from'), end=q.get('to'), unknown=q.get('unknown') == 'true',
            offset=number(q, 'offset', 0, 20000), limit=number(q, 'limit', 50, 100), scope=q.get('scope'),
            collection=number(q, 'collection', None), volume=number(q, 'volume', None), provider=q.get('provider'),
            precision=q.get('precision'), kind=q.get('kind'), ownership=q.get('ownership'))
        if result['latest_sync']:
            result['latest_sync'] = current_app.extensions['release_calendar'].status(result['latest_sync']['id'])
        return result

    @api.route('/calendar/events/<string:identity>', methods=['GET'])
    @route
    def calendar_detail(identity):
        query(())
        if not fullmatch(r'(issue|publication):[1-9][0-9]{0,18}', identity):
            raise CalendarError('invalid_request')
        return CalendarStore(get_db()).detail(identity)

    @api.route('/calendar/refresh', methods=['POST'])
    @route
    def calendar_refresh():
        query(())
        return current_app.extensions['release_calendar'].submit(**body(('provider',)))

    @api.route('/calendar/tasks/<string:identity>', methods=['GET'])
    @route
    def calendar_task(identity):
        query(())
        if not fullmatch('[0-9a-f]{32}', identity):
            raise CalendarError('invalid_request')
        return current_app.extensions['release_calendar'].status(identity)
