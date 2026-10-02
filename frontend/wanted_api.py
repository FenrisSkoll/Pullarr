"""Authenticated automation controls. No client locator or candidate authority."""

from flask import current_app, request


def register(api, auth, error_handler, return_api):
    from backend.base.organization_job import OrganizationError
    from backend.features.wanted_automation import WantedAutomation
    from backend.features.wanted_status import search_history, wanted_rows
    from backend.implementations.organization_filesystem import execution_gate
    from backend.internals.db import DBConnection
    from backend.internals.wanted import WantedConflict
    from backend.internals.wanted_configuration import (load_automation,
                                                        save_automation)

    @api.route('/wanted', methods=['GET', 'POST'])
    @api.route('/wanted/history', methods=['GET'])
    @api.route('/wanted/configuration', methods=['GET', 'POST'])
    @error_handler
    @auth
    def wanted_api():
        service = WantedAutomation(DBConnection.default_file)
        try:
            if request.method == 'GET':
                if request.path.endswith('/configuration'):
                    runtime = current_app.extensions.get('wanted_runtime')
                    return return_api({'configuration': load_automation(service.store.db),
                                       'source_backoff': [dict(r) for r in service.store.db.execute('SELECT * FROM wanted_source_retry ORDER BY source_key')],
                                       'discovery': [dict(r) for r in service.store.db.execute('SELECT * FROM wanted_discovery ORDER BY source_key')],
                                       'health': runtime.health() if runtime else {'running': False}})
                if request.path.endswith('/history'):
                    return return_api(search_history(service.store, request.args.get('volume_id', type=int)))
                return return_api(wanted_rows(service.store, offset=request.args.get('offset', 0, type=int),
                                             state=request.args.get('state')))
            data = request.get_json(silent=True)
            with execution_gate(service.store.path + '.wanted'):
                if request.path.endswith('/configuration'):
                    return return_api(save_automation(service.store.db, data))
                if (not isinstance(data, dict) or set(data) - {'action', 'volume_id', 'issue_id', 'decision_id', 'acknowledge_duplicate_risk'}):
                    raise WantedConflict('invalid_action')
                if data.get('action') == 'search':
                    if type(data.get('volume_id')) is not int or (data.get('issue_id') is not None and type(data['issue_id']) is not int):
                        raise WantedConflict('invalid_target')
                    return return_api({'queued': service.store.request_search(data['volume_id'], data.get('issue_id'))})
                if data.get('action') == 'release_review' and data.get('acknowledge_duplicate_risk') is True:
                    # Deliberate operator abandonment does not delete/cancel artifacts/jobs.
                    decision = data.get('decision_id')
                    if not isinstance(decision, str):
                        raise WantedConflict('invalid_decision')
                    row = service.store.db.execute('SELECT state FROM wanted_decisions WHERE id=?', (decision,)).fetchone()
                    if not row or row[0] != 'review':
                        raise WantedConflict('not_review')
                    service.store.transition(decision, 'abandoned', error='operator_released_duplicate_risk_acknowledged')
                    return return_api({'state': 'abandoned'})
                raise WantedConflict('invalid_action')
        except (WantedConflict, OrganizationError, ValueError, TypeError):
            return return_api({'reason': 'action_unavailable'}, error='WantedFailure', code=409)
        finally:
            service.close()
