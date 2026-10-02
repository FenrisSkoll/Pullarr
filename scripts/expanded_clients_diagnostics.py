"""Bounded synthetic protocol diagnostics; no network or personal configuration."""
import json
import sys
import tracemalloc
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from Tbackend.features.release_scoring import target
from TExpandedClients import config
from TManagedClientsAPI import ManagedClientAPITests
from TTorznab import feed

from backend.base.acquisition_intake import DownloaderPathMapping
from backend.base.managed_client import RetentionPolicy, retention_evaluation
from backend.base.release_search import ReleaseSearchRequest, SourceConfig
from backend.implementations.acquisition_paths import map_download_path
from backend.implementations.download_transport import PrivateResponse
from backend.implementations.nzbget import NZBGetClient
from backend.implementations.qbittorrent import QBittorrentClient
from backend.implementations.release_scoring import evaluate_release
from backend.implementations.torznab import TorznabSource
from backend.internals.download_jobs import DownloadStore
from backend.internals.quality import QualityStore


def measure(name, operation, requests):
    tracemalloc.start()
    start = perf_counter()
    result = operation()
    elapsed = perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    print(json.dumps(dict(name=name, seconds=round(elapsed, 6), peak_bytes=peak,
        selects=0, client_requests=requests(), result_count=len(result))), flush=True)


def main():
    for count in (100, 500):
        item = feed().split(b'<item>')[1].split(b'</item>')[0]
        payload = (b'<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>' +
                   b''.join(b'<item>' + item.replace(b'stable-fixture',str(i).encode()) + b'</item>' for i in range(count)) +
                   b'</channel></rss>')
        class SourceTransport:
            calls = 0
            def get(self, *args):
                self.calls += 1
                return payload
        transport = SourceTransport()
        source = TorznabSource(SourceConfig('fixture','Fixture','http://fixture/api','synthetic',mode='torznab'),transport)
        def search():
            rows = source.search(ReleaseSearchRequest('Batman')).candidates
            return [evaluate_release(target(),row) for row in rows]
        measure(f'torznab_parse_normalize_evaluate_{count}',search,lambda:transport.calls)

    class ClientTransport:
        calls = 0
        count = 100
        def request(self,url,**kwargs):
            self.calls += 1
            if '/jsonrpc' in url:
                method = json.loads(kwargs['body'])['method']
                result = [dict(NZBID=i+1,Status='DOWNLOADING') for i in range(self.count)] if method=='listgroups' else []
                if self.history:
                    result = [] if method=='listgroups' else [dict(NZBID=i+1,Kind='NZB',Status='SUCCESS/ALL',DestDir='/complete') for i in range(self.count)]
                return PrivateResponse(200,json.dumps(dict(id=1,result=result)).encode())
            path = urlsplit(url).path
            if path.endswith('/auth/login'):
                return PrivateResponse(200,b'Ok.',cookie='SID=synthetic-session-id; HttpOnly')
            if path.endswith('/files'):
                result = [dict(index=i,name=f'Comic {i:03}.cbz',priority=1,progress=1,size=1000) for i in range(500)]
            else:
                hashes = parse_qs(urlsplit(url).query)['hashes'][0].split('|')
                result = [dict(hash=h,state='uploading',progress=1,ratio=.5,seeding_time=60) for h in hashes]
            return PrivateResponse(200,json.dumps(result).encode())

    for count in (100,1000):
        transport = ClientTransport()
        client = QBittorrentClient(config('qbittorrent'),transport)
        hashes = [f'{i:040x}' for i in range(count)]
        measure(f'qbit_info_{count}',lambda:[row for offset in range(0,count,100)
            for row in client.info(tuple(hashes[offset:offset+100]))],lambda:transport.calls)
    transport = ClientTransport(); client = QBittorrentClient(config('qbittorrent'),transport)
    measure('qbit_files_500',lambda:client.files('a'*40),lambda:transport.calls)
    for count, history in ((100,False),(500,True)):
        transport = ClientTransport(); transport.count=count; transport.history=history
        client = NZBGetClient(config(),transport)
        measure(f'nzbget_{"history" if history else "queue"}_{count}',
            lambda:client.observe(tuple(str(i+1) for i in range(count))),lambda:transport.calls)
    measure('seeding_policy_1000',lambda:[retention_evaluation(RetentionPolicy('both','1',60),
        current_ratio='.5',seeding_seconds=100,requirements=dict(seedtype='either',minimumratio='1',minimumseedtime=60))
        for _ in range(1000)],lambda:0)
    fixture = ManagedClientAPITests(); fixture.setUp()
    try:
        db = fixture.fixture.db
        mapping = DownloaderPathMapping('fixture','client','instance','/complete',str(fixture.fixture.incoming))
        measure('path_mapping_500',lambda:[map_download_path(f'/complete/Comic {i:03}.cbz','client','instance',(mapping,))
            for i in range(500)],lambda:0)
        intent = json.dumps(dict(title='Synthetic release',source_name='Fixture',client_kind='qbittorrent',protocol='torrent'))
        db.executemany('''INSERT INTO acquisition_downloads
            (id,intent_digest,intent,client_id,client_instance,state,nzo_id,created_at,updated_at)
            VALUES(?, 'fixture', ?, 'fixture', 'instance', 'submitted', ?, '2026-10-01', '2026-10-01')''',
            ((f'{i:032x}',intent,f'{i:040x}') for i in range(1000)))
        selected = []
        def traced_store(path):
            store = DownloadStore(path)
            store.db.set_trace_callback(lambda sql:selected.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
            return store
        with patch('frontend.managed_clients_api.DownloadStore',side_effect=traced_store):
            start = perf_counter()
            result = fixture.request('GET','/managed-downloads?limit=50')
            print(json.dumps(dict(name='activity_50_of_1000',seconds=round(perf_counter()-start,6),
                selects=len(selected),json_bytes=len(json.dumps(result).encode()),client_requests=0)),flush=True)
        quality = QualityStore(db.cursor())
        candidate = SimpleNamespace(raw_title='Batman 001 (2020) (Digital)',
            source=SimpleNamespace(key='fixture',kind=SimpleNamespace(value='torznab')),candidate_id='fixture')
        for _ in range(50): quality.selected(1,candidate,reason='manual',decision={})
        selected.clear()
        db.set_trace_callback(lambda sql:selected.append(sql) if sql.lstrip().upper().startswith('SELECT') else None)
        start = perf_counter(); result = quality.history(1,limit=50)
        print(json.dumps(dict(name='history_50',seconds=round(perf_counter()-start,6),selects=len(selected),
            json_bytes=len(json.dumps(result).encode()),client_requests=0)),flush=True)
    finally:
        fixture.doCleanups()


if __name__ == '__main__':
    main()
