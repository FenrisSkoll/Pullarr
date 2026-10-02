"""Explicit disposable Docker acceptance bootstrap; never the production CMD."""
import os
import sys
from pathlib import Path

sys.path[:0] = ['/app', '/app/tests']

from fixtures.discovery import seed

from backend.internals.collections import CollectionStore
from backend.internals.db import get_db, set_db_location, setup_db
from backend.internals.reading_orders import ReadingOrderStore
from backend.internals.server import Server
from backend.internals.settings import Settings


def main():
    assert os.environ.get('PULLARR_DISPOSABLE_FIXTURE') == '1'
    set_db_location('/app/db')
    app = Server().app
    with app.app_context():
        setup_db()
        Settings().update({'api_key': 'disposable-pullarr-ui',
                           'download_folder': '/app/temp_downloads',
                           'db_backup_folder': '/app/db'})
        db = get_db()
        if not db.execute('SELECT 1 FROM volumes').fetchone():
            seed(db, Path('/fixture-data'))
            collection = CollectionStore(db)
            group = collection.create('Demo publication collection')
            collection.add_local(group['nodes'][0]['id'], group['revision'], 1)
            orders = ReadingOrderStore(db)
            order = orders.create('Demo reading sequence')
            orders.add_local(order['id'], order['revision'], 1)
            db.connection.commit()
    os.execv(sys.executable, [sys.executable, '/app/Pullarr.py', '-d', '/app/db',
                             '-l', '/app/logs', '-t', '/app/temp_downloads'])


if __name__ == '__main__':
    main()
