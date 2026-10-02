"""Authenticated strict transport. Provider evidence is always server-owned."""

import json
from functools import wraps
from re import fullmatch

from flask import current_app, request

from backend.base.collections import CollectionError, integer, text
from backend.base.logging import LOGGER
from backend.internals.collections import CollectionStore
from backend.internals.db import get_db


def body(keys, *, exact=True):
    if not request.is_json:
        raise CollectionError('invalid_request')
    raw = request.stream.read(65537)
    if len(raw) > 65536:
        raise CollectionError('bounded')
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise CollectionError('invalid_request')
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=unique)
    if not isinstance(value, dict) or (set(value) != set(keys) if exact else bool(set(value) - set(keys))):
        raise CollectionError('invalid_request')
    return value


def query(keys):
    if len(request.query_string) > 8192:
        raise CollectionError('bounded')
    if set(request.args) - set(keys) - {'api_key'} or any(len(request.args.getlist(k)) != 1 for k in request.args):
        raise CollectionError('invalid_request')
    return {k: v for k, v in request.args.items() if k != 'api_key'}


def number(q, key, default=0, maximum=2**63 - 1):
    value = q.get(key)
    if value is None:
        return default
    if not fullmatch(r'0|[1-9][0-9]{0,18}', value):
        raise CollectionError('invalid_request')
    return integer(int(value), 0, maximum)


def register(api, auth, error_handler, return_api):
    def route(function):
        @wraps(function)
        def safe(*args, **kwargs):
            try:
                for value in kwargs.values():
                    if type(value) is int:
                        integer(value)
                if request.content_length and request.content_length > 65536:
                    raise CollectionError('bounded')
                result = function(*args, **kwargs)
                if len(json.dumps(result).encode()) > 2 * 1024 * 1024:
                    raise CollectionError('bounded')
                return return_api(result)
            except CollectionError as error:
                reason = str(error)
                if reason not in {'invalid_request', 'bounded', 'not_found', 'revision_conflict', 'invalid_tree',
                    'invalid_parent', 'duplicate_sibling_title', 'publication_identity_conflict', 'invalid_reference',
                    'different_collection', 'confirmation_required', 'unsupported_evidence', 'search_expired', 'capacity', 'task_unavailable'}:
                    reason = 'invalid_request'
                return return_api({'reason': reason}, 'CollectionsFailure', 404 if reason == 'not_found' else
                    410 if reason == 'search_expired' else 409 if reason in ('revision_conflict', 'bounded', 'publication_identity_conflict') else 400)
            except (ValueError, TypeError, UnicodeError):
                return return_api({'reason': 'invalid_request'}, 'CollectionsFailure', 400)
            except Exception:
                LOGGER.error('Collections transport failed unexpectedly')
                return return_api({'reason': 'internal_error'}, 'CollectionsFailure', 500)
        return error_handler(auth(safe))

    def store():
        return CollectionStore(get_db())

    def owner():
        return current_app.extensions['collections']

    @api.route('/collections', methods=['GET', 'POST'])
    @route
    def collections_index():
        if request.method == 'POST':
            query(())
            return store().create(**body(('title', 'description', 'monitoring')))
        q = query(('after', 'limit'))
        return store().page(number(q, 'after'), number(q, 'limit', 50, 100))

    @api.route('/collections/<int:collection>', methods=['GET'])
    @route
    def collections_tree(collection):
        query(())
        return store().tree(collection)

    @api.route('/collections/<int:collection>/nodes', methods=['POST'])
    @route
    def collections_node(collection):
        query(())
        b = body(('revision', 'node_id', 'title', 'description', 'kind', 'monitoring', 'parent_id', 'position'))
        for key in ('node_id', 'parent_id'):
            if b[key] is not None:
                integer(b[key])
        return store().edit_node(collection, **b)

    @api.route('/collections/<int:collection>/nodes/<int:node>/delete', methods=['POST'])
    @route
    def collections_delete(collection, node):
        query(())
        return store().delete_node(collection, node_id=node, **body(('revision', 'confirmed')))

    @api.route('/collections/<int:collection>/publications', methods=['GET'])
    @route
    def collections_publications(collection):
        q = query(('node_id', 'offset', 'limit'))
        return store().publications(collection, number(q, 'node_id', None), number(q, 'offset', 0, 2000), number(q, 'limit', 50, 100))

    @api.route('/collections/nodes/<int:node>/local', methods=['POST'])
    @route
    def collections_local(node):
        query(())
        return store().add_local(node, **body(('revision', 'volume_id')))

    @api.route('/collections/nodes/<int:node>/membership', methods=['POST'])
    @route
    def collections_membership(node):
        query(())
        return store().membership(node, **body(('revision', 'publication', 'action', 'target', 'note', 'position')))

    @api.route('/collections/nodes/<int:node>/kind', methods=['POST'])
    @route
    def collections_kind(node):
        query(())
        return store().edit_publication(node, **body(('revision', 'publication', 'kind')))

    @api.route('/collections/nodes/<int:node>/search', methods=['POST'])
    @route
    def collections_search(node):
        query(())
        return owner().submit_search(node, **body(('query', 'provider', 'suggestions')))

    @api.route('/collections/tasks/<string:identifier>', methods=['GET'])
    @route
    def collections_task(identifier):
        if not fullmatch('[0-9a-f]{32}', identifier):
            raise CollectionError('invalid_request')
        q = query(('offset', 'limit'))
        return owner().delivery(identifier, number(q, 'offset', 0, 750), number(q, 'limit', 50, 100))

    @api.route('/collections/tasks/<string:identifier>/propose', methods=['POST'])
    @route
    def collections_propose(identifier):
        query(())
        b = body(('result_key',))
        return owner().propose_result(text(identifier, 32), text(b['result_key'], 128))

    @api.route('/collections/nodes/<int:node>/suggestions', methods=['GET'])
    @route
    def collections_suggestions(node):
        q = query(('decision', 'offset', 'limit'))
        return store().suggestions(node, q.get('decision', 'pending'), number(q, 'offset', 0, 2000), number(q, 'limit', 50, 100))

    @api.route('/collections/suggestions/<string:identifier>/decision', methods=['POST'])
    @route
    def collections_decision(identifier):
        query(())
        if not fullmatch('[0-9a-f]{64}', identifier):
            raise CollectionError('invalid_request')
        return store().decide(identifier, **body(('revision', 'collection_revision', 'decision')))

    @api.route('/collections/publications/<int:publication>/add', methods=['POST'])
    @route
    def collections_add(publication):
        query(())
        return owner().submit_add(publication, **body(('root_id', 'provider', 'confirmed')))

    @api.route('/collections/discovery', methods=['GET'])
    @route
    def collections_discovery():
        q = query(('after', 'limit'))
        return store().calendar_page(number(q, 'after'), number(q, 'limit', 50, 100))

    @api.route('/collections/discovery/nodes', methods=['GET'])
    @route
    def collections_discovery_nodes():
        q = query(('after', 'limit'))
        return store().monitored_nodes_page(number(q, 'after'), number(q, 'limit', 50, 100))
