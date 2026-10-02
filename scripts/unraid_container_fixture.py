"""Synthetic UID99 storage/intake checks; injected only into disposable containers."""
import json
import os
import shutil
import sqlite3
import sys
from dataclasses import fields
from hashlib import sha256
from pathlib import Path

sys.path[:0] = ['/app', '/tmp/acceptance/tests']

from TArchiveMaintenance import comic
from Tbackend.features.organization_plan import NAMING

from backend.base.acquisition_intake import (AcquisitionCompletion,
                                             AcquisitionKind)
from backend.base.definitions import SpecialVersion
from backend.base.organization_job import JobState
from backend.features.acquisition_intake import IntakeCoordinator
from backend.features.organization_archive import register_archive, review
from backend.features.organization_execution import OrganizationExecutor
from backend.implementations.archive_normalization import inspect
from backend.internals.acquisition_intakes import ensure_intake
from backend.internals.db import DB_SCHEMA


def intake(name, kind, *, rar=False, split=False):
    library = Path('/data/media/comics')/name
    folder = library/'Batman'
    folder.mkdir(parents=True)
    incoming = Path('/split' if split else '/data/downloads')/name
    incoming.mkdir(parents=True)
    source = incoming/('Batman 001 (2020)'+('.cbr' if rar else '.cbz'))
    comic(source, rar)
    original = sha256(source.read_bytes()).hexdigest()
    database = Path('/data/acceptance')/(name+'.db')
    database.parent.mkdir(exist_ok=True)
    db = sqlite3.connect(database, isolation_level=None)
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(DB_SCHEMA)
    db.execute("INSERT INTO config VALUES('database_version',72)")
    db.executemany('INSERT INTO config VALUES(?,?)', ((f.name,getattr(NAMING,f.name)) for f in fields(NAMING)))
    db.execute('INSERT INTO root_folders VALUES(1,?)',(str(library),))
    db.execute('''INSERT INTO volumes(id,comicvine_id,title,year,volume_number,publisher,root_folder,folder,custom_folder,special_version)
        VALUES(1,101,'Batman',2020,1,'Synthetic',1,?,0,?)''',(str(folder),SpecialVersion.NORMAL.value))
    db.execute("INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,title,date) VALUES(1,1,201,'1',1,'Fixture','2020-01-02')")
    # Equal-prefix mapping is explicit admission of a visible client root, not a mount.
    db.execute('''INSERT INTO acquisition_path_mappings(id,client_id,client_instance,remote_prefix,remote_style,local_root)
        VALUES('map','fixture','instance',?,'posix',?)''',(str(incoming),str(incoming)))
    completion = AcquisitionCompletion(kind,name,'candidate',1,(1,),(str(source),),'2026-10-02T12:00:00+00:00',
        client_id='fixture',client_instance='instance',remote_job_id='a'*40,mechanism='torrent' if kind==AcquisitionKind.QBITTORRENT else 'usenet')
    identifier = ensure_intake(db,completion,rename=True,auto_apply=True)
    clock = [100.]
    coordinator = IntakeCoordinator(str(database),clock=lambda:clock[0])
    try:
        for _ in range(8):
            result = coordinator.process(identifier)
            clock[0] += 11
        assert result['state']=='completed', result
        fid, target_name = db.execute('SELECT id,filepath FROM active_files').fetchone()
        target = Path(target_name)
        assert (target.stat().st_uid,target.stat().st_gid)==(99,100)
        assert sha256(target.read_bytes()).hexdigest()==original
        method = None
        if kind==AcquisitionKind.QBITTORRENT:
            assert source.exists() and sha256(source.read_bytes()).hexdigest()==original
            method = db.execute('SELECT import_method FROM acquisition_seed_artifacts').fetchone()[0]
            assert method == ('copy' if split else 'hardlink'), method
            assert os.path.samefile(source,target) is (not split)
            if split: assert source.stat().st_dev != target.stat().st_dev
        if rar or name=='torrents':
            executor = OrganizationExecutor(str(database),('/data','/split'))
            try:
                _, confirmation = review(executor,fid)
                job = executor.apply_job(register_archive(executor,fid,confirmation,name+'-normalize'))
                assert job.state == JobState.COMPLETED, job.error
                final = Path(db.execute('SELECT filepath FROM active_files WHERE id=?',(fid,)).fetchone()[0])
                assert final.suffix == '.cbz' and inspect(str(final))['status']=='healthy'
                assert (final.stat().st_uid,final.stat().st_gid)==(99,100)
                if kind==AcquisitionKind.QBITTORRENT:
                    assert sha256(source.read_bytes()).hexdigest()==original and not os.path.samefile(source,final)
            finally: executor.close()
        assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
        assert not db.execute('PRAGMA foreign_key_check').fetchall()
        return dict(case=name,kind=kind.value,import_method=method,owned=True,uid=99,gid=100,
                    maintenance=bool(rar or name=='torrents'),source_preserved=kind==AcquisitionKind.QBITTORRENT)
    finally:
        coordinator.close()
        db.close()


if __name__ == '__main__':
    assert (os.getuid(),os.getgid())==(99,100)
    Path('/app/temp_downloads/permission-probe').write_text('synthetic')
    results = [intake('torrents',AcquisitionKind.QBITTORRENT),
               intake('seeded-cbr',AcquisitionKind.QBITTORRENT,rar=True),
               intake('cross-device',AcquisitionKind.QBITTORRENT,split=True),
               intake('sab',AcquisitionKind.SABNZBD),intake('nzbget',AcquisitionKind.NZBGET)]
    print(json.dumps(results))
