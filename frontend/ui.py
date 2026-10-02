# -*- coding: utf-8 -*-

from io import BytesIO
from json import dumps
from typing import Any

from flask import Blueprint, redirect, render_template, request, send_file

from backend.internals.server import Server

ui = Blueprint('ui', __name__)
methods = ['GET']
ALLOWED_THEMES = ["", "dark-mode"]


def render(filename: str, **kwargs: Any) -> str:
    theme = ''
    if "theme" in request.cookies and request.cookies["theme"] in ALLOWED_THEMES:
        theme = request.cookies["theme"]

    return render_template(
        filename,
        url_base=Server.url_base,
        theme=theme,
        **kwargs
    )


@ui.route('/manifest.json', methods=methods)
def ui_manifest():
    return send_file(
        BytesIO(dumps(
            {
                "name": "Pullarr",
                "short_name": "Pullarr",
                "description": "Manage, monitor and organize your comic library.",
                "display": "standalone",
                "orientation": "portrait-primary",
                "start_url": f"{Server.url_base}/",
                "scope": f"{Server.url_base}/",
                "id": f"{Server.url_base}/",
                "background_color": "#f5f7f8",
                "theme_color": "#087f78",
                "icons": [
                    {
                        "src": f"{Server.url_base}/static/img/favicon.svg",
                        "type": "image/svg+xml",
                        "sizes": "any"
                    }
                ]
            },
            indent=4
        ).encode('utf-8')),
        mimetype="application/manifest+json",
        download_name="manifest.json"
    ), 200


@ui.route('/login', methods=methods)
def ui_login():
    return render('login.html')


@ui.route('/', methods=methods)
def ui_volumes():
    return render('volumes.html')


@ui.route('/add', methods=methods)
def ui_add_volume():
    return render('add_volume.html')


@ui.route('/library-import', methods=methods)
def ui_library_import():
    return render('library_import.html')


@ui.route('/maintenance', methods=methods)
def ui_maintenance():
    return render('maintenance.html')


@ui.route('/collections', methods=methods)
def ui_collections():
    return render('collections.html')


@ui.route('/calendar', methods=methods)
def ui_calendar():
    return render('calendar.html')


@ui.route('/discover', methods=methods)
def ui_discover():
    return render('discover.html')


@ui.route('/reading-orders', methods=methods)
def ui_reading_orders():
    return render('reading_orders.html')


@ui.route('/settings/quality', methods=methods)
def ui_quality():
    return render('quality.html')


@ui.route('/volumes/<id>', methods=methods)
def ui_view_volume(id):
    return render('view_volume.html')


@ui.route('/volumes/<int:id>/provider-switch', methods=methods)
def ui_provider_switch(id):
    return render('provider_switch.html', volume_id=id)


@ui.route('/activity/queue', methods=methods)
def ui_queue():
    return render('queue.html')


@ui.route('/activity/history', methods=methods)
def ui_history():
    return render('history.html')


@ui.route('/activity/intake', methods=methods)
def ui_intake():
    return render('intake.html')


@ui.route('/wanted', methods=methods)
def ui_wanted():
    return render('wanted.html')


@ui.route('/activity/blocklist', methods=methods)
def ui_blocklist():
    return render('blocklist.html')


@ui.route('/system/status', methods=methods)
def ui_status():
    return render('status.html')


@ui.route('/system/tasks', methods=methods)
def ui_tasks():
    return render('tasks.html')


@ui.route('/system/backups', methods=methods)
def ui_backup():
    return render('backups.html')


@ui.route('/settings', methods=methods)
def ui_settings():
    return redirect(f'{Server.url_base}/settings/mediamanagement')


@ui.route('/settings/mediamanagement', methods=methods)
def ui_mediamanagement():
    return render('settings_mediamanagement.html')


@ui.route('/settings/indexers', methods=methods)
def ui_indexers():
    return render('settings_indexers.html')


@ui.route('/settings/download', methods=methods)
def ui_download():
    return render('settings_download.html')


@ui.route('/settings/downloadclients', methods=methods)
def ui_download_clients():
    return render('settings_download_clients.html')


@ui.route('/settings/metadata', methods=methods)
def ui_metadata():
    return render('settings_metadata.html')


@ui.route('/settings/general', methods=methods)
def ui_general():
    return render('settings_general.html')
