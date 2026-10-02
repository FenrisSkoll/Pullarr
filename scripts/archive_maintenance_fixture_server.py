"""Disposable Docker browser fixture; never part of normal application startup."""
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]

from TArchiveMaintenance import comic
from werkzeug.serving import WSGIRequestHandler, make_server

from backend.internals.db import get_db, set_db_location, setup_db
from backend.internals.server import Server
from backend.internals.settings import Settings


class Quiet(WSGIRequestHandler):
    def log(self,*args,**kwargs):pass


def main():
    with TemporaryDirectory(prefix='pullarr-9b-container-') as temporary:
        root=Path(temporary);folder=root/'library'/'Fixture';folder.mkdir(parents=True)
        set_db_location(str(root/'db'));server=Server()
        with server.app.app_context():
            setup_db();Settings().update({'api_key':'synthetic-archive-container'})
            db=get_db();db.execute('UPDATE indexer_clients SET enabled=0')
            db.execute('INSERT INTO root_folders VALUES(1,?)',(str(folder.parent),))
            db.execute("INSERT INTO volumes(id,comicvine_id,title,year,volume_number,root_folder,folder) VALUES(1,101,'Synthetic archive fixture',2020,1,1,?)",(str(folder),))
            db.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,NULL)')
            for index,name in enumerate(('ordinary.cbr','shared.cbr','healthy.cbz'),1):
                path=folder/name;comic(path,index<3)
                db.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number) VALUES(?,1,?,?,?)',(index,200+index,str(index),index))
                db.execute('INSERT INTO files(id,filepath,size) VALUES(?,?,?)',(index,str(path),path.stat().st_size))
                db.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(?,?)',(index,index))
            os.link(folder/'shared.cbr',root/'seed.cbr')
            db.connection.commit()
        make_server('0.0.0.0',5656,server.app,threaded=True,request_handler=Quiet).serve_forever()


if __name__=='__main__':main()
