"""Loopback-only deterministic NZBGet/qBittorrent/Torznab service contracts."""
import base64
import json
from contextlib import contextmanager
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit

from backend.base.managed_client import ManagedClientConfig
from backend.base.release_search import SourceConfig

CAPS = b'<caps><limits default="100" max="100"/><searching><search available="yes" supportedParams="q"/></searching><categories><category id="7000" name="Books"><subcat id="7030" name="Comics"/></category></categories></caps>'
HASH = 'a' * 40
SID = 'synthetic-session-12345678'


@contextmanager
def services():
    state = dict(calls=[], submitted=0, completed=False, removed=False, ratio=.5, seeding_time=60,
                 title='Batman 001 (2020) (HD-Digital)', files=['Batman 001 (2020).cbz'], label='HD-Digital', hash=HASH)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, value, status=200, cookie=None):
            payload = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(payload)))
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            parsed = urlsplit(self.path)
            path = parsed.path
            data = self.rfile.read(min(int(self.headers.get('Content-Length', '0')), 8 * 1024 * 1024))
            state['calls'].append((self.command, path))  # No headers, keys, bodies.
            if path == '/nzb':
                from Tbackend.features.sab_downloads import NZB
                return self.reply(NZB)
            if path == '/newznab/api':
                from Tbackend.features.release_search import item, rss
                if parse_qs(parsed.query).get('t') == ['caps']:
                    return self.reply(CAPS)
                return self.reply(rss(item(title=state['title'], guid='nzbget-release', url=state['url'] + '/nzb')))
            if path == '/torznab/api':
                query = parse_qs(parsed.query)
                if query.get('t') == ['caps']:
                    return self.reply(CAPS)
                return self.reply((f'<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel><item>'
                    f'<title>{state["title"]}</title><guid>fixture-release</guid>'
                    f'<enclosure url="magnet:?xt=urn:btih:{state["hash"]}" length="1000"/>'
                    '<torznab:attr name="seeders" value="5"/><torznab:attr name="minimumratio" value="1"/>'
                    '<torznab:attr name="seedtype" value="ratio"/></item>'
                    '<torznab:response offset="0" total="1"/></channel></rss>').encode())
            if path == '/nzbget/jsonrpc':
                expected = 'Basic ' + base64.b64encode(b'fixture-user:fixture-password').decode()
                if self.headers.get('Authorization') != expected:
                    return self.reply({}, 401)
                request = json.loads(data)
                method = request['method']
                if method == 'version': result = '25.3'
                elif method == 'append':
                    state['submitted'] += 1
                    result = 42
                elif method == 'listgroups': result = [] if state['completed'] else [dict(NZBID=42, Status='DOWNLOADING')]
                elif method == 'history': result = [dict(NZBID=42, Kind='NZB', Status=state.get('nzb_status','SUCCESS/ALL'), DestDir='/complete')] if state['completed'] else []
                else: return self.reply(dict(id=request['id'], error=dict(code=1)))
                return self.reply(dict(id=request['id'], result=result))
            if path == '/qbit/api/v2/auth/login':
                values = parse_qs(data.decode())
                if values != {'username':['fixture-user'],'password':['fixture-password']}:
                    return self.reply(b'Fails.', 403)
                return self.reply(b'Ok.', cookie='SID=' + SID + '; HttpOnly; Path=/')
            if self.headers.get('Cookie') != 'SID=' + SID:
                return self.reply({}, 403)
            endpoint = path.removeprefix('/qbit/api/v2/')
            if endpoint == 'app/version': return self.reply(b'v5.0.0')
            if endpoint == 'app/webapiVersion': return self.reply(b'2.11.0')
            if endpoint == 'torrents/categories': return self.reply({'pullarr': {'name':'pullarr','savePath':'/complete'}})
            if endpoint == 'torrents/add':
                if self.command != 'POST' or b'magnet:?xt=urn:btih:' not in data:
                    return self.reply(b'Fails.')
                state['submitted'] += 1
                return self.reply(b'Ok.')
            if endpoint == 'torrents/info':
                return self.reply([] if not state['submitted'] or state['removed'] else [dict(hash=state['hash'],
                    category='pullarr', state='uploading' if state['completed'] else 'downloading',
                    progress=1 if state['completed'] else .5, save_path='/complete',
                    ratio=state['ratio'], seeding_time=state['seeding_time'], size=1000, dlspeed=0, upspeed=0)])
            if endpoint == 'torrents/properties': return self.reply(dict(infohash_v1=state['hash'], infohash_v2=''))
            if endpoint == 'torrents/files': return self.reply([dict(index=n,name=name,priority=1,progress=1,size=1000)
                                                                  for n,name in enumerate(state['files'])])
            if endpoint == 'torrents/delete':
                state['removed'] = True
                state['delete_data'] = parse_qs(data.decode()).get('deleteFiles') == ['true']
                return self.reply(b'')
            return self.reply({}, 404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state['url'] = 'http://127.0.0.1:' + str(server.server_address[1])
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def configure(db, service, incoming, *, kind='qbittorrent'):
    mode = 'torznab' if kind == 'qbittorrent' else 'newznab'
    source = SourceConfig('torrent-fixture','Source fixture',service['url'] + '/' + mode + '/api','fixture-key',mode=mode)
    value = asdict(source); value.pop('key')
    db.execute('INSERT INTO config VALUES(?,?)', ('release_source_v1:' + source.key, json.dumps(value)))
    client = ManagedClientConfig('torrent-fixture', kind + ' fixture', service['url'] + ('/qbit' if kind == 'qbittorrent' else '/nzbget'),
                                'fixture-user','fixture-password',kind=kind)
    value = asdict(client); value.pop('key')
    db.execute('INSERT INTO config VALUES(?,?)', ('managed_client_v1:' + client.key,json.dumps(value)))
    db.execute('''INSERT INTO acquisition_path_mappings
        (id,client_id,client_instance,remote_prefix,remote_style,local_root)
        VALUES('torrent-map',?,?,'/complete','posix',?)''', (client.key,client.instance,str(incoming)))
    return client
