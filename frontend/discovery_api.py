"""Authenticated, strict Discover transport; no client source URLs or payloads."""

import json
from functools import wraps
from re import fullmatch

from flask import current_app, request

from backend.base.collections import CollectionError
from backend.base.discovery import DiscoveryError, integer
from backend.base.logging import LOGGER
from backend.internals.db import get_db
from backend.internals.discovery import DiscoveryStore
from frontend.collections_api import body, number, query


def register(api, auth, error_handler, return_api):
    def route(fn):
        @wraps(fn)
        def safe(*args, **kwargs):
            try:
                if request.content_length and request.content_length > 16384:
                    raise DiscoveryError('bounded')
                for value in kwargs.values():
                    if type(value) is int:
                        integer(value)
                    elif not fullmatch('[0-9a-f]{32}', value):
                        raise DiscoveryError('invalid_request')
                result = fn(*args, **kwargs)
                if len(json.dumps(result).encode()) > 1024*1024:
                    raise DiscoveryError('bounded')
                return return_api(result)
            except (DiscoveryError, CollectionError) as error:
                reason = str(error)
                allowed = {'invalid_request','bounded','not_found','revision_conflict','capacity','source_disabled',
                    'rate_limited','task_expired','matching_scope_bounded','invalid_query','invalid_body'}
                if reason not in allowed:
                    reason = 'invalid_request'
                return return_api(dict(reason=reason), 'DiscoverFailure', 404 if reason == 'not_found' else
                    410 if reason == 'task_expired' else 409 if reason in ('revision_conflict','capacity') else 400)
            except (ValueError, TypeError, KeyError, UnicodeError):
                return return_api(dict(reason='invalid_request'), 'DiscoverFailure', 400)
            except Exception:
                LOGGER.error('Discover request failed unexpectedly')
                return return_api(dict(reason='internal_error'), 'DiscoverFailure', 500)
        return error_handler(auth(safe))

    def owner():
        return current_app.extensions['discover']

    @api.route('/discover', methods=['GET'])
    @route
    def discover_page():
        q = query(('offset','limit','q','category','year','quality','state'))
        return owner().page(offset=number(q,'offset'),limit=number(q,'limit',50,100),
            **{k:v for k,v in q.items() if k not in ('offset','limit')})

    @api.route('/discover/status', methods=['GET'])
    @route
    def discover_status():
        query(())
        return DiscoveryStore(get_db()).status()

    @api.route('/discover/settings', methods=['POST'])
    @route
    def discover_settings():
        query(())
        return DiscoveryStore(get_db()).settings(**body(('revision','enabled','automatic','interval_minutes')))

    @api.route('/discover/refresh', methods=['POST'])
    @route
    def discover_refresh():
        query(())
        body(())
        return owner().submit()

    @api.route('/discover/tasks/<string:identifier>', methods=['GET'])
    @route
    def discover_task(identifier):
        query(())
        return owner().status(identifier)

    @api.route('/discover/posts/<int:identifier>', methods=['GET'])
    @route
    def discover_post(identifier):
        query(())
        return owner().detail(identifier)

    @api.route('/discover/posts/<int:identifier>/acquisition-preview', methods=['POST'])
    @route
    def discover_preview(identifier):
        query(())
        body(())
        DiscoveryStore(get_db()).post(identifier)
        return owner().submit(operation='preview',post_id=identifier)

    @api.route('/discover/posts/<int:identifier>/acquire', methods=['POST'])
    @route
    def discover_acquire(identifier):
        query(())
        value = body(('preview_id','offering_id','confirmed'))
        if value['confirmed'] is not True or not isinstance(value['preview_id'],str) or not fullmatch('[0-9a-f]{32}',value['preview_id']) or not isinstance(value['offering_id'],str) or not fullmatch('[0-9a-f]{64}',value['offering_id']):
            raise DiscoveryError('invalid_request')
        return owner().submit(operation='acquire',post_id=identifier,confirmation=value)
