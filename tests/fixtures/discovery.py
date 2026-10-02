"""Synthetic fixed-origin discovery transport and loopback DDL bytes.

Only test/bootstrap code installs these seams; no production HTTP option does.
"""

from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from xml.sax.saxutils import escape
from zipfile import ZipFile

from PIL import Image
from Tbackend.features.release_search import fake_http

from backend.base.definitions import DownloadClientIdentifier
from backend.base.discovery import FEED, ORIGIN, DiscoveryError


def archive(edge, issue):
    page, output = BytesIO(), BytesIO()
    Image.new('RGB',(edge,edge*2),'white').save(page,format='PNG')
    with ZipFile(output,'w') as z:
        for i in range(3):z.writestr(f'{i}.png',page.getvalue())
        z.writestr('ComicInfo.xml',f'<ComicInfo><Series>Batman</Series><Number>{issue}</Number><Year>2020</Year></ComicInfo>')
    return output.getvalue()


class FixtureSource:
    def __init__(self):
        self.posts=[(1,'Batman #1 (2020) (HD-Digital)'),(2,'Batman #2 (2020) (HD-Digital)'),
            (3,'Batman #3 (2020) (HD-Digital)'),(4,'Unknown #1 (2026)'),(5,'2026.09.30 Weekly Pack')]
        self.mode='feed';self.calls=[];self.version=1
        self.changed_offering=False

    def get(self,url,etag=None,modified=None):
        self.calls.append((url,etag,modified))
        if self.mode=='outage':raise DiscoveryError('network_unavailable')
        if url==FEED:
            if self.mode=='fallback':raise DiscoveryError('invalid_feed')
            if etag==f'fixture-{self.version}':return dict(unchanged=True)
            data=('<rss version="2.0"><channel>'+''.join(
                f'<item><title>{escape(title)}</title><link>{ORIGIN}/other-comics/{key}/</link>'
                f'<guid isPermaLink="false">fixture:{key}</guid><category>Other Comics</category>'
                '<pubDate>Wed, 30 Sep 2026 21:18:52 +0000</pubDate>'
                '<description>&lt;p&gt;Year : 2020 | Size : 12 MB&lt;/p&gt;</description></item>'
                for key,title in self.posts)+'</channel></rss>').encode()
        else:
            data=('<html>'+''.join(f'<article class="post"><h1 class="post-title"><a href="{ORIGIN}/other-comics/{key}/">{escape(title)}</a></h1><a class="post-category">Other Comics</a><time datetime="2026-09-30">Today</time></article>' for key,title in self.posts)+'</html>').encode()
        return dict(unchanged=False,data=data,etag=f'fixture-{self.version}',last_modified=None)

    def fetch(self,base,url):
        self.calls.append((url,None,None))
        issue=int(url.rstrip('/').split('/')[-1])
        title=f'Batman #{issue} (2020) (HD-Digital)'
        if self.changed_offering:title += ' changed'
        return f'<section class="post-contents"><ul><li>{title}<a href="{ORIGIN}/file/{issue}.cbz">Main Server</a></li></ul></section>'


@contextmanager
def source_fixture():
    source=FixtureSource()
    def reply(path,query):
        issue=int(Path(path).stem)
        return 200,archive(900 if issue==3 else 1200,issue),{'Content-Disposition':f'attachment; filename="Batman {issue:03} (2020).cbz"'}
    with fake_http(reply, daemon=True) as (origin,calls):
        def mirror(service,link,validate):
            if not validate(link) or not link.startswith(ORIGIN+'/file/'):
                raise AssertionError('Unexpected fixture mirror')
            return origin+'/file/'+link.rsplit('/',1)[-1],DownloadClientIdentifier.DDL
        with patch('backend.implementations.direct_download_source.resolve_mirror',side_effect=mirror):
            yield source,calls


def seed(cursor,base,quality=True):
    from TQuality import policy

    from backend.implementations.file_quality import analyze
    from backend.internals.quality import QualityStore

    library=base/'library';folder=library/'Batman';incoming=base/'downloads'
    folder.mkdir(parents=True,exist_ok=True);incoming.mkdir(exist_ok=True)
    cursor.execute('INSERT INTO root_folders VALUES(1,?)',(str(library),))
    cursor.execute("INSERT INTO volumes(id,comicvine_id,title,year,volume_number,root_folder,folder,monitored) VALUES(1,101,'Batman',2020,1,1,?,1)",(str(folder),))
    cursor.execute('INSERT INTO volumes_covers(volume_id,cover) VALUES(1,NULL)')
    for issue in range(1,4):
        cursor.execute('INSERT INTO issues(id,volume_id,comicvine_id,issue_number,calculated_issue_number,monitored) VALUES(?,1,?,?,?,1)',(issue,200+issue,str(issue),issue))
        if issue>1:
            path=folder/f'Batman {issue:03}.cbz';path.write_bytes(archive(600,issue))
            cursor.execute('INSERT INTO files(id,filepath,size) VALUES(?,?,?)',(issue,str(path),path.stat().st_size))
            cursor.execute('INSERT INTO issues_files(file_id,issue_id) VALUES(?,?)',(issue,issue))
    cursor.connection.commit()
    if quality:
        store=QualityStore(cursor)
        store.save('Fixture quality',policy(),identifier=1,revision=1)
        for issue in (2,3):
            store.assessment(issue,analyze(str(folder/f'Batman {issue:03}.cbz')))
    return folder,incoming
