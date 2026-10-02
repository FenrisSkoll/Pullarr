"""Strict authenticated Reading Order transport; identifiers, not client evidence."""

import json
from functools import wraps
from re import fullmatch

from flask import Response, current_app, request

from backend.base.collections import CollectionError, integer
from backend.base.logging import LOGGER
from backend.base.reading_orders import MAX_BYTES, ReadingOrderError
from backend.internals.db import get_db
from backend.internals.reading_orders import ReadingOrderStore
from frontend.collections_api import body, number, query


def register(api, auth, error_handler, return_api):
    def route(fn):
        @wraps(fn)
        def safe(*args, **kwargs):
            try:
                for value in kwargs.values():
                    if type(value) is int:
                        integer(value)
                    elif not fullmatch('[0-9a-f]{32}', value):
                        raise ReadingOrderError('invalid_request')
                if request.content_length and request.content_length > MAX_BYTES:
                    raise ReadingOrderError('bounded')
                value = fn(*args, **kwargs)
                if isinstance(value, Response):
                    return value, value.status_code
                if len(json.dumps(value).encode()) > 2*MAX_BYTES:
                    raise ReadingOrderError('bounded')
                return return_api(value)
            except CollectionError as error:
                reason = str(error)
                if reason not in ('invalid_request', 'bounded', 'not_found', 'revision_conflict', 'detach_required', 'confirmation_required',
                    'unsafe_xml', 'invalid_xml', 'unsupported_cbl', 'unsupported_source', 'blocked_destination', 'source_exists', 'stale',
                    'review_expired', 'capacity', 'task_unavailable', 'unsupported_source_order', 'requires_exact_publication', 'invalid_reference'):
                    reason = 'invalid_request'
                return return_api({'reason': reason}, 'ReadingOrdersFailure', 404 if reason == 'not_found' else 410 if reason == 'review_expired'
                    else 409 if reason in ('revision_conflict', 'stale', 'detach_required', 'capacity') else 400)
            except (ValueError, TypeError, UnicodeError):
                return return_api({'reason': 'invalid_request'}, 'ReadingOrdersFailure', 400)
            except Exception:
                LOGGER.error('Reading Orders request failed unexpectedly')
                return return_api({'reason': 'internal_error'}, 'ReadingOrdersFailure', 500)
        return error_handler(auth(safe))

    def owner():
        return current_app.extensions['reading_orders']

    def store():
        return ReadingOrderStore(get_db())

    def confirmation(b):
        if type(b.get('revision', 0)) is not int or b.get('revision', 0) < 0:
            raise ReadingOrderError('invalid_request')
        if not isinstance(b.get('expected_digest'), str) or not fullmatch('[0-9a-f]{64}', b['expected_digest']):
            raise ReadingOrderError('invalid_request')
        return b

    @api.route('/reading-orders', methods=['GET', 'POST'])
    @route
    def reading_orders_index():
        if request.method == 'POST':
            query(())
            return store().create(**body(('title', 'description')))
        q = query(('offset', 'limit'))
        return store().page(number(q, 'offset'), number(q, 'limit', 50, 100))

    @api.route('/reading-orders/<int:order_id>', methods=['GET', 'POST'])
    @route
    def reading_orders_detail(order_id):
        query(())
        if request.method == 'POST':
            return store().edit(order_id, **body(('revision', 'title', 'description')))
        return store().get(order_id)

    @api.route('/reading-orders/<int:order_id>/delete', methods=['POST'])
    @route
    def reading_orders_delete(order_id):
        query(())
        return store().delete(order_id, **body(('revision', 'confirmed')))

    @api.route('/reading-orders/<int:order_id>/entries', methods=['GET', 'POST'])
    @route
    def reading_orders_entries(order_id):
        if request.method == 'POST':
            query(())
            return store().add_local(order_id, **body(('revision', 'issue_id')))
        q = query(('offset', 'limit', 'status'))
        return store().entries(order_id, number(q, 'offset'), number(q, 'limit', 50, 100), q.get('status', 'all'))

    @api.route('/reading-orders/<int:order_id>/reorder', methods=['POST'])
    @route
    def reading_orders_reorder(order_id):
        query(())
        return store().move(order_id, **body(('revision', 'entry_id', 'position')))

    @api.route('/reading-orders/<int:order_id>/remove', methods=['POST'])
    @route
    def reading_orders_remove(order_id):
        query(())
        return store().remove(order_id, **body(('revision', 'entry_id')))

    @api.route('/reading-orders/<int:order_id>/resolve', methods=['POST'])
    @route
    def reading_orders_resolve(order_id):
        query(())
        return store().resolve(order_id, **body(('revision', 'entry_id', 'issue_id')))

    @api.route('/reading-orders/local-issues', methods=['GET'])
    @route
    def reading_orders_local():
        q = query(('query', 'offset', 'limit'))
        return store().local_issues(q.get('query', ''), number(q, 'offset'), number(q, 'limit', 50, 100))

    @api.route('/reading-orders/import', methods=['POST'])
    @route
    def reading_orders_import():
        query(())
        if request.mimetype not in ('application/xml', 'text/xml', 'application/octet-stream'):
            raise ReadingOrderError('invalid_request')
        return owner().upload(request.stream.read(MAX_BYTES+1))

    @api.route('/reading-orders/reviews/<string:handle>', methods=['GET'])
    @api.route('/reading-orders/tasks/<string:handle>', methods=['GET'])
    @route
    def reading_orders_review(handle):
        q = query(('offset', 'limit'))
        return owner().delivery(handle, number(q, 'offset'), number(q, 'limit', 50, 100))

    @api.route('/reading-orders/reviews/<string:handle>/resolve', methods=['POST'])
    @route
    def reading_orders_review_resolve(handle):
        query(())
        return owner().resolve_review(handle, **body(('revision', 'position', 'issue_id')))

    @api.route('/reading-orders/reviews/<string:handle>/accept', methods=['POST'])
    @route
    def reading_orders_review_accept(handle):
        query(())
        return owner().accept(handle, **confirmation(body(('revision', 'expected_digest', 'confirmed'))))

    @api.route('/reading-orders/<int:order_id>/export', methods=['GET'])
    @route
    def reading_orders_export(order_id):
        query(())
        return Response(store().export(order_id), content_type='application/xml; charset=utf-8',
            headers={'Content-Disposition': f'attachment; filename="reading-order-{order_id}.cbl"', 'X-Content-Type-Options': 'nosniff'})

    @api.route('/reading-orders/<int:order_id>/subscriptions', methods=['POST'])
    @route
    def reading_orders_subscribe(order_id):
        query(())
        b = body(('revision', 'url'))
        return store().attach(order_id, b['revision'], 'cbl_url', b['url'])

    @api.route('/reading-orders/<int:order_id>/detach', methods=['POST'])
    @route
    def reading_orders_detach(order_id):
        query(())
        return store().detach(order_id, **body(('revision', 'confirmed')))

    @api.route('/reading-orders/sources/<int:source_id>/refresh', methods=['POST'])
    @route
    def reading_orders_refresh(source_id):
        query(())
        body(())
        return owner().refresh(source_id)

    @api.route('/reading-orders/sources/<int:source_id>/pending', methods=['GET'])
    @route
    def reading_orders_pending(source_id):
        q = query(('offset', 'limit'))
        return store().pending(source_id, number(q, 'offset'), number(q, 'limit', 50, 100))

    @api.route('/reading-orders/sources/<int:source_id>/decision', methods=['POST'])
    @route
    def reading_orders_decision(source_id):
        query(())
        b = confirmation(body(('revision', 'order_revision', 'expected_digest', 'decision', 'confirmed')))
        integer(b['order_revision'], 0)
        return store().decide_source(source_id, **b)

    @api.route('/reading-orders/providers/search', methods=['POST'])
    @route
    def reading_orders_provider_search():
        query(())
        return owner().search(**body(('provider', 'query')))

    @api.route('/reading-orders/providers/<string:handle>/fetch', methods=['POST'])
    @route
    def reading_orders_provider_fetch(handle):
        query(())
        return owner().fetch_provider(handle, **body(('result_id',)))

    @api.route('/reading-orders/<int:order_id>/add-publication', methods=['POST'])
    @route
    def reading_orders_add(order_id):
        query(())
        return owner().add(order_id, **body(('entry_id', 'provider', 'root_id', 'confirmed')))

    @api.route('/reading-orders/<int:order_id>/wanted-preview', methods=['POST'])
    @route
    def reading_orders_wanted(order_id):
        query(())
        return owner().wanted_preview(order_id, **body(('revision', 'entries')))

    @api.route('/reading-orders/wanted/<string:handle>/apply', methods=['POST'])
    @route
    def reading_orders_wanted_apply(handle):
        query(())
        return owner().wanted_apply(handle, **confirmation(body(('expected_digest', 'selected', 'confirmed'))))
