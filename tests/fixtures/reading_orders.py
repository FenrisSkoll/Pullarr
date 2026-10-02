"""Synthetic CBL/provider transport for disposable runtime acceptance only."""

from hashlib import sha256
from io import BytesIO

from backend.base.reading_orders import entry

HOSTILE = '<img src=x onerror="window.readingOrderHostile=true">'
CBL = b'''<?xml version="1.0" encoding="utf-8"?><ReadingList><Name>Fixture &lt;script&gt;window.readingOrderHostile=true&lt;/script&gt;</Name><Description>Safe text</Description><Books>
<Book Series="One" Number="1" Volume="2026"><Database Name="cv" Series="100" Issue="101"/></Book>
<Book Series="One" Number="2" Volume="2026"><Database Name="cv" Series="100" Issue="102"/></Book>
<Book Series="One" Number="3" Volume="2026"><Database Name="cv" Series="100" Issue="103"/></Book>
<Book Series="External" Number="1"><Database Name="cv" Series="900" Issue="901"/></Book>
<Book Series="One" Number="3" Volume="2026"/>
<Book Series="Unknown" Number="Special"/>
<Book Series="One" Number="1" Volume="2026"><Database Name="cv" Series="100" Issue="101"/></Book>
</Books></ReadingList>'''


class FixtureLists:
    def search(self, query):
        return [dict(id='42', title='Fixture List '+HOSTILE, provider='metron')]

    def fetch(self, identity):
        model = dict(title='Provider Ordered Fixture', description='', warnings=[], entries=[
            entry('One', '2', refs=[dict(provider='comicvine', issue_id='102', volume_id='100')]),
            entry('One', '1', refs=[dict(provider='comicvine', issue_id='101', volume_id='100')])])
        return dict(model=model, digest=sha256(str(model).encode()).hexdigest(), etag=None, last_modified=None)


class FixtureResponse:
    def __init__(self, data, unchanged):
        self.status = 304 if unchanged else 200
        self.data = BytesIO(data)
        self.headers = {'Content-Type': 'application/xml', 'ETag': sha256(data).hexdigest()}

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        return self.data.read(size)


class FixtureConnection:
    data = CBL
    sock = None

    def __init__(self, host, address, timeout):
        assert address == '93.184.216.34'

    def request(self, method, path, headers):
        self.unchanged = headers.get('If-None-Match') == sha256(self.data).hexdigest()

    def getresponse(self):
        return FixtureResponse(self.data, self.unchanged)

    def close(self):
        pass
