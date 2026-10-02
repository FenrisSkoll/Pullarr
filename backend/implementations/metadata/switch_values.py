"""Restore admitted immutable review values for existing persistence primitives.

No acquisition or new normalization: interpreted facts retain their exact policy,
precision, provenance and raw observations from admission.
"""

from types import SimpleNamespace

from backend.base.bibliography import (CreditObservation, EditionFacts,
                                       IssueBibliography, PublicationFacts,
                                       StoryMode, StoryObservation)
from backend.base.issue_facts import (BibliographicDate, DateKind,
                                      DatePrecision, IssueFacts,
                                      IssueNumberFacts, NumberKind)


def facts(value):
    number = dict(value['number'])
    number['interpretation'] = NumberKind(number['interpretation'])
    dates = tuple(BibliographicDate(**dict(row, kind=DateKind(row['kind']),
        precision=DatePrecision(row['precision']))) for row in value['dates'])
    return IssueFacts(**dict(value, number=IssueNumberFacts(**number), dates=dates))


def bibliography(value):
    if value is None:
        return None
    edition = EditionFacts(**dict(value['edition'], supplied=tuple(value['edition']['supplied'])))
    stories = tuple(StoryObservation(**dict(row, mode=StoryMode(row['mode']),
        credits=tuple(CreditObservation(**credit) for credit in row['credits']))) for row in value['stories'])
    return IssueBibliography(**dict(value, edition=edition, stories=stories,
                                  diagnostics=tuple(value['diagnostics'])))


def bibliography_snapshot(remote, provider):
    publication = remote['bibliography']
    if publication is not None:
        publication = PublicationFacts(**dict(publication, supplied=tuple(publication['supplied']),
                                             diagnostics=tuple(publication['diagnostics'])))
    return SimpleNamespace(volume=SimpleNamespace(provider=provider), publication=publication,
        receipt=SimpleNamespace(acquired_at=remote['receipt']['acquired_at']),
        issues=tuple(SimpleNamespace(provider_id=row['provider_id'], bibliography=bibliography(row['bibliography']))
                     for row in remote['issues']))
