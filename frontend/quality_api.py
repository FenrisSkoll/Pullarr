"""Authenticated quality policy, bounded observations and provenance projections."""

import json
from functools import wraps
from re import fullmatch

from flask import current_app, request

from backend.base.collections import CollectionError
from backend.base.logging import LOGGER
from backend.base.quality import QualityError, integer
from backend.internals.db import get_db
from backend.internals.quality import QualityStore
from frontend.collections_api import body, number, query


def register(api, auth, error_handler, return_api):
    def route(fn):
        @wraps(fn)
        def safe(*args, **kwargs):
            try:
                for value in kwargs.values():
                    if type(value) is int:
                        integer(value, 1)
                    elif not fullmatch('[0-9a-f]{32}', value):
                        raise QualityError('invalid_request')
                if request.content_length and request.content_length > 65536:
                    raise QualityError('bounded')
                result = fn(*args, **kwargs)
                if len(json.dumps(result).encode()) > 1024*1024:
                    raise QualityError('bounded')
                return return_api(result)
            except (QualityError, CollectionError) as error:
                reason = str(error)
                if reason not in {'invalid_request', 'invalid_policy', 'invalid_groups', 'invalid_cutoff', 'bounded',
                    'not_found', 'revision_conflict', 'confirmation_required', 'profile_in_use', 'task_expired', 'capacity'}:
                    reason = 'invalid_request'
                return return_api({'reason': reason}, 'QualityFailure', 404 if reason == 'not_found' else
                    410 if reason == 'task_expired' else 409 if reason in ('revision_conflict','profile_in_use','capacity') else 400)
            except (ValueError, TypeError, KeyError, UnicodeError):
                return return_api({'reason': 'invalid_request'}, 'QualityFailure', 400)
            except Exception:
                LOGGER.error('Quality request failed unexpectedly')
                return return_api({'reason': 'internal_error'}, 'QualityFailure', 500)
        return error_handler(auth(safe))

    def store():
        return QualityStore(get_db())

    @api.route('/quality-profiles', methods=['GET', 'POST'])
    @route
    def quality_profiles():
        query(())
        if request.method == 'POST':
            return store().save(**body(('name', 'policy')))
        return dict(items=store().profiles(), default=store().default())

    @api.route('/quality-profiles/<int:identifier>', methods=['GET', 'POST'])
    @route
    def quality_profile(identifier):
        query(())
        if request.method == 'POST':
            return store().save(identifier=identifier, **body(('name', 'policy', 'revision')))
        return store().profile(identifier)

    @api.route('/quality-profiles/<int:identifier>/delete', methods=['POST'])
    @route
    def quality_profile_delete(identifier):
        query(())
        return store().delete(identifier, **body(('revision', 'confirmed')))

    @api.route('/quality-profiles/default', methods=['POST'])
    @route
    def quality_default():
        query(())
        return store().set_default(**body(('profile_id', 'revision')))

    @api.route('/volumes/<int:identifier>/quality', methods=['GET', 'POST'])
    @route
    def quality_volume(identifier):
        if not get_db().execute('SELECT 1 FROM volumes WHERE id=?', (identifier,)).fetchone():
            raise QualityError('not_found')
        if request.method == 'POST':
            query(())
            return store().assign('volume', identifier, **body(('profile_id', 'expected_profile_id')))
        q = query(('offset', 'limit'))
        return dict(assignment=store().effective([identifier])[identifier],
                    **store().issue_page(identifier, number(q,'offset'), number(q,'limit',50,100)))

    @api.route('/collections/nodes/<int:identifier>/quality', methods=['GET', 'POST'])
    @route
    def quality_node(identifier):
        query(())
        if not get_db().execute('SELECT 1 FROM collection_nodes WHERE id=?', (identifier,)).fetchone():
            raise QualityError('not_found')
        if request.method == 'POST':
            return store().assign('node', identifier, **body(('profile_id', 'expected_profile_id')))
        row = get_db().execute('SELECT profile_id FROM collection_quality_profiles WHERE node_id=?', (identifier,)).fetchone()
        return dict(profile_id=row[0] if row else None)

    @api.route('/issues/<int:identifier>/quality', methods=['GET'])
    @route
    def quality_issue(identifier):
        query(())
        values = store().issue_states([identifier])
        if not values:
            raise QualityError('not_found')
        return values[0]

    @api.route('/issues/<int:identifier>/acquisitions', methods=['GET'])
    @route
    def quality_history(identifier):
        q = query(('offset', 'limit'))
        return store().history(identifier, number(q,'offset'), number(q,'limit',50,100))

    @api.route('/acquisitions/<string:identifier>', methods=['GET'])
    @route
    def quality_acquisition(identifier):
        query(())
        return store().detail(identifier)

    @api.route('/quality-analysis', methods=['POST'])
    @route
    def quality_analysis():
        query(())
        return current_app.extensions['quality_analysis'].submit(**body(('file_ids',)))

    @api.route('/quality-analysis/<string:identifier>', methods=['GET'])
    @route
    def quality_analysis_status(identifier):
        query(())
        return current_app.extensions['quality_analysis'].status(identifier)
