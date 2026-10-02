"""Deterministic provider, injectable only by tests; no transport or credentials."""

from copy import deepcopy
from dataclasses import replace

from backend.implementations.metadata.models import (IssueMetadata,
                                                     VolumeMetadata)
from backend.implementations.metadata.provider import (
    MetadataBulkVolumeProvider, MetadataVolumeProvider)


class NeutralProvider(MetadataVolumeProvider, MetadataBulkVolumeProvider):
    def __init__(self):
        self.calls = []
        self.issues = [self.issue('I:one', '1'), self.issue('I:two', '2')]
        self.title = 'Neutral Series'

    def issue(self, identity, number, title='TPB'):
        return IssueMetadata('test_provider', identity, 'V:alpha', number,
                             float(number), title, '2021-01-01', None)

    def volume(self):
        return VolumeMetadata('test_provider', 'V:alpha', self.title, 2021, 1,
                              None, None, None, '', ['Alias'], None,
                              len(self.issues), False, deepcopy(self.issues))

    async def fetch_volume(self, identity):
        self.calls.append(('single', identity))
        return self.volume()

    async def fetch_volumes(self, identities):
        self.calls.append(('volumes', tuple(identities)))
        return [replace(self.volume(), issues=None)]

    async def fetch_issues(self, identities):
        self.calls.append(('issues', tuple(identities)))
        return deepcopy(self.issues)
