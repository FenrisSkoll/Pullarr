"""Disposable container-only fixture server; never use with persistent appdata."""
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from fixtures.expanded_clients import configure, services
from fixtures.quality import comic
from TQuality import policy
from werkzeug.serving import WSGIRequestHandler, make_server

from backend.features.intake_runtime import IntakeRuntime
from backend.features.sab_downloads import poll_downloads
from backend.features.torrent_lifecycle import observe_torrents
from backend.features.wanted_runtime import WantedRuntime
from backend.implementations.file_quality import analyze
from backend.implementations.managed_clients import client_for
from backend.internals.db import (DBConnection, get_db,
                                  set_db_location, setup_db)
from backend.internals.download_jobs import DownloadStore
from backend.internals.quality import QualityStore
from backend.internals.server import Server
from backend.internals.settings import Settings


class QuietHandler(WSGIRequestHandler):
    def log(self,*args,**kwargs):
        pass


def main():
    with TemporaryDirectory(prefix='pullarr-9a-container-') as directory, services() as remote:
        root = Path(directory)
        folder, incoming = root/'library/Batman', root/'incoming'
        folder.mkdir(parents=True); incoming.mkdir()
        set_db_location(str(root/'db'))
        server = Server()
        with server.app.app_context():
            setup_db(); Settings().update({'api_key':'synthetic-container-fixture'})
            db = get_db(); db.execute('UPDATE indexer_clients SET enabled=0')
            db.execute('INSERT INTO root_folders VALUES(1,?)',(str(folder.parent),))
            db.execute("INSERT INTO volumes(id,comicvine_id,title,year,volume_number,root_folder,folder,monitored) VALUES(1,101,'Batman',2020,1,1,?,1)",(str(folder),))
            db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,NULL)')
            db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(1,1,201,'1',1,1)")
            old = folder/'Batman 001.cbz'; comic(old,600)
            comic(incoming/remote['files'][0],1200)
            db.execute('INSERT INTO files(id,filepath,size) VALUES(1,?,?)',(str(old),old.stat().st_size))
            db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(1,1)')
            quality = QualityStore(db); quality.save('Container upgrade fixture',policy(),identifier=1,revision=1)
            quality.assessment(1,analyze(str(old)))
            client = configure(db,remote,incoming)
            db.connection.commit()
        stop = Event()
        def worker():
            wanted = WantedRuntime(DBConnection.default_file,server.app)
            runtime = IntakeRuntime(DBConnection.default_file)
            while not stop.wait(.25):
                wanted.tick()
                remote['completed'] = True
                store = DownloadStore(DBConnection.default_file)
                try:
                    poll_downloads(store,[client],client_factory=client_for)
                    observe_torrents(store,[client],client_for)
                finally:
                    store.close()
                runtime.tick()
        Thread(target=worker,daemon=True).start()
        http = make_server('0.0.0.0',5656,server.app,threaded=True,request_handler=QuietHandler)
        print('Disposable Phase 9A container fixture ready',flush=True)
        try:
            http.serve_forever()
        finally:
            stop.set(); http.server_close()


if __name__ == '__main__':
    main()
