"""Explicit disposable normal-entrypoint fixture; never on production sys.path."""

import atexit
from unittest.mock import patch

from fixtures.discovery import source_fixture

from backend.features.discovery import Discover

_context=source_fixture()
_source,_calls=_context.__enter__()
atexit.register(lambda:_context.__exit__(None,None,None))
_init=Discover.__init__


def fixture_init(self,**kwargs):
    kwargs['transport']=_source
    _init(self,**kwargs)


_patch=patch.object(Discover,'__init__',fixture_init)
_patch.start()
