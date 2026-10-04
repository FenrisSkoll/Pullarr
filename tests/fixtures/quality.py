"""Synthetic archives and real loopback indexer/SAB; no personal sources."""

import json
import re
from contextlib import contextmanager
from dataclasses import asdict
from io import BytesIO

from PIL import Image
from Tbackend.features.release_search import CAPS, config, fake_http, item, rss
from Tbackend.features.sab_downloads import NZB, SAB_KEY, fake_sab

from backend.base.download_job import SABConfig
from backend.internals.wanted_configuration import save_automation


def comic(path, edge, pages=3, codec='PNG'):
    from zipfile import ZipFile
    data=BytesIO()
    Image.new('RGB',(edge,edge*2),'white').save(data,format=codec)
    with ZipFile(path,'w') as archive:
        for n in range(pages):
            archive.writestr(f'{n:04}.png' if codec=='PNG' else f'{n:04}.jpg',data.getvalue())


@contextmanager
def sources():
    state={'issue':1,'label':'HD-Digital'}
    def response(path,query):
        if path=='/nzb':
            return 200,NZB,{}
        if query.get('t')==['caps']:
            return 200,CAPS,{}
        requested=query.get('q',[''])[0]
        numbers=re.findall(r'(?<!\d)([1-3])(?!\d)',requested)
        number=int(numbers[-1]) if numbers else state['issue']
        label = '' if state.get('omit_issue') else f' #{number}'
        return 200,rss(item(title=f'Batman{label} (2020) ({state["label"]}).cbz',
            guid=f'quality-{number}-{state["label"]}',url=indexer+'/nzb')),{}
    with fake_http(response) as (indexer,calls),fake_sab() as (sab,remote):
        yield dict(indexer=indexer,sab=sab,remote=remote,calls=calls,state=state)


def configure(db,source,incoming):
    indexer=asdict(config(url=source['indexer']+'/api'))
    key=indexer.pop('key')
    db.execute('INSERT INTO config VALUES(?,?)',('release_source_v1:'+key,json.dumps(indexer)))
    client=SABConfig('quality-fixture','Quality fixture',source['sab'],SAB_KEY,True,'comics',-100)
    private=asdict(client);private.pop('key')
    db.execute('INSERT INTO config VALUES(?,?)',('sab_client_v1:'+client.key,json.dumps(private)))
    db.execute('''INSERT INTO acquisition_path_mappings
        (id,client_id,client_instance,remote_prefix,remote_style,local_root,enabled,local_prefix)
        VALUES(?,?,?,'/complete','posix',?,1,NULL)''',('quality-map',client.key,client.instance,str(incoming)))
    save_automation(db,{'mode':'off','sab_client_id':client.key})
    return client
