"""Authenticated bounded configuration, not a generic client proxy."""
import json
from functools import wraps
from re import fullmatch

from flask import request

from backend.base.collections import CollectionError
from backend.base.download_job import DownloadErrorCode as E, DownloadFailure
from backend.base.logging import LOGGER
from backend.implementations.managed_clients import client_for
from backend.internals.db import DBConnection
from backend.internals.download_jobs import DownloadStore
from backend.internals.managed_clients import (delete_client,
                                               load_managed_clients,
                                               save_client)
from frontend.collections_api import body, number, query


def register(api, auth, error_handler, return_api):
    def route(function):
        @wraps(function)
        def safe(*args, **kwargs):
            try:
                query(('offset','limit') if request.endpoint.endswith('managed_downloads') else ())
                if request.content_length and request.content_length > 65536:
                    raise CollectionError('bounded')
                return return_api(function(*args, **kwargs))
            except DownloadFailure as error:
                return return_api({'code': error.code.value}, 'ClientFailure', 409 if error.code in (E.DRIFT, E.BUSY) else 400)
            except (CollectionError, ValueError, TypeError, UnicodeError, RecursionError):
                return return_api({'code': 'invalid_request'}, 'ClientFailure', 400)
            except Exception:
                LOGGER.error('Managed client operation failed unexpectedly; durable state retained')
                return return_api({'code': 'internal_error'}, 'ClientFailure', 500)
        return error_handler(auth(safe))

    def selected(identifier, revision):
        if not fullmatch(r'[a-zA-Z0-9_-]{1,64}', identifier):
            raise DownloadFailure(E.CONFIGURATION)
        config = next((c for c in load_managed_clients() if c.key == identifier), None)
        if config is None:
            raise DownloadFailure(E.CONFIGURATION)
        if revision != config.revision:
            raise DownloadFailure(E.DRIFT)
        return config

    @api.route('/managed-clients', methods=['GET', 'POST'])
    @route
    def managed_clients():
        if request.method == 'GET':
            return [c.preview() for c in load_managed_clients()]
        value = body(('configuration',))
        return save_client(value['configuration']).preview()

    @api.route('/managed-clients/<identifier>', methods=['PUT', 'DELETE'])
    @route
    def managed_client(identifier):
        value = body(('revision', 'configuration') if request.method == 'PUT' else ('revision',))
        selected(identifier, value['revision'])
        if request.method == 'DELETE':
            delete_client(identifier, expected_revision=value['revision'])
            return {}
        return save_client(value['configuration'], identifier, expected_revision=value['revision']).preview()

    @api.route('/managed-clients/<identifier>/test', methods=['POST'])
    @route
    def managed_client_test(identifier):
        value = body(('revision',))
        return client_for(selected(identifier, value['revision'])).check()

    @api.route('/managed-downloads', methods=['GET'])
    @route
    def managed_downloads():
        q = query(('offset','limit'))
        offset, limit = number(q,'offset',0,100000), number(q,'limit',50,100)
        if limit < 1:
            raise CollectionError('invalid_request')
        store = DownloadStore(DBConnection.default_file)
        try:
            rows = store.db.execute('''SELECT d.*,t.state torrent_state,t.observation torrent_observation,
                t.policy,t.requirements,i.state intake_state FROM acquisition_downloads d
                LEFT JOIN acquisition_torrents t ON t.download_id=d.id
                LEFT JOIN acquisition_intakes i ON i.download_id=d.id
                    AND i.kind=COALESCE(json_extract(d.intent,'$.client_kind'),'sabnzbd')
                ORDER BY d.created_at DESC,d.id LIMIT ? OFFSET ?''',(limit,offset)).fetchall()
            result = []
            for row in rows:
                intent = json.loads(row['intent'])
                observation = json.loads(row['observation'])
                result.append(dict(id=row['id'], title=intent['title'], source=intent['source_name'],
                    client=intent.get('client_kind','sabnzbd'), protocol=intent.get('protocol','nzb'),
                    acquisition=row['intake_state'] or row['state'], state=row['state'], error=row['error'],
                    observation={key: observation[key] for key in ('progress','size','speed','status') if key in observation},
                    torrent_state=row['torrent_state'],
                    torrent=json.loads(row['torrent_observation'] or '{}'),
                    retention=json.loads(row['policy'] or '{}'), requirements=json.loads(row['requirements'] or '{}')))
            return result
        finally:
            store.close()

    def torrent_action(identifier, execute):
        from backend.features.torrent_lifecycle import (cleanup,
                                                        cleanup_preview,
                                                        observe_torrents)
        if not fullmatch('[0-9a-f]{32}', identifier):
            raise CollectionError('invalid_request')
        value = body(('delete_data','confirmation') if execute else ('delete_data',))
        if type(value['delete_data']) is not bool or execute and not isinstance(value['confirmation'],str):
            raise CollectionError('invalid_request')
        store = DownloadStore(DBConnection.default_file)
        try:
            row = store.get(identifier)
            config = next((c for c in load_managed_clients(store.db) if c.key==row['client_id']
                and c.instance==row['client_instance'] and c.kind=='qbittorrent' and c.enabled),None)
            if config is None:
                raise DownloadFailure(E.DRIFT)
            client = client_for(config)
            if execute:
                return cleanup(store,identifier,config,client,value['confirmation'],delete_data=value['delete_data'],manual=True)
            observe_torrents(store,(config,),lambda _:client)
            result = cleanup_preview(store,identifier,delete_data=value['delete_data'],manual=True)
            return {key: result[key] for key in ('eligible','reasons','policy','confirmation','delete_data')}
        finally:
            store.close()

    @api.route('/managed-downloads/<identifier>/cleanup-preview', methods=['POST'])
    @route
    def torrent_cleanup_preview(identifier):
        return torrent_action(identifier,False)

    @api.route('/managed-downloads/<identifier>/cleanup', methods=['POST'])
    @route
    def torrent_cleanup(identifier):
        return torrent_action(identifier,True)
