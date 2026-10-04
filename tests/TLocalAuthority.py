"""Explicit publication authority never supplies invented issue coverage."""
from dataclasses import replace
from unittest import TestCase

from Tbackend.features.identification import (candidate, claimed, comic,
                                              issue, reference, volume)

from backend.base.definitions import SpecialVersion
from backend.base.identification import MatchState, PublicationAuthority
from backend.base.import_candidate import ResourceKind
from backend.implementations.identification import (MatchingSnapshot,
                                                    identify_authorized)


class LocalAuthorityTests(TestCase):
    def test_each_selected_namespace_is_preserved(self):
        for provider in ('comicvine', 'metron', 'gcd'):
            with self.subTest(provider=provider):
                selected = volume(authority=reference('103802',provider))
                snapshot = MatchingSnapshot.build([selected],[issue()])
                result = identify_authorized(candidate(),snapshot,1,PublicationAuthority.IMPORT_SELECTION)
                self.assertEqual(result.state,MatchState.AUTOMATIC)
                self.assertEqual(result.selected.provider_identity, selected.authority)
                self.assertEqual(result.selected.local_issue_ids,(1,))

    def test_exact_embedded_conflict_retains_publication_for_review(self):
        snapshot=MatchingSnapshot.build([volume()],[issue()])
        result=identify_authorized(claimed(candidate(),reference('different')),snapshot,1,PublicationAuthority.MANAGED_VOLUME)
        self.assertEqual(result.state,MatchState.REVIEW)
        self.assertEqual(result.selected.local_volume_id,1)

    def test_embedded_issue_precedes_filename_and_duplicate_numbers_remain_review(self):
        ref=reference('exact-issue',kind=ResourceKind.ISSUE)
        snapshot=MatchingSnapshot.build([volume()],[issue(1,references=(ref,)),issue(2)])
        result=identify_authorized(claimed(candidate(number=None),ref),snapshot,1,PublicationAuthority.IMPORT_SELECTION)
        self.assertEqual(result.selected.local_issue_ids,(1,))
        self.assertEqual(result.state,MatchState.AUTOMATIC)
        ambiguous=identify_authorized(candidate(),snapshot,1,PublicationAuthority.IMPORT_SELECTION)
        self.assertEqual(ambiguous.state,MatchState.REVIEW)
        self.assertEqual(ambiguous.selected.local_issue_ids,())

    def test_explicit_filename_issue_precedes_descriptive_book_number(self):
        snapshot=MatchingSnapshot.build([volume(special_version=SpecialVersion.VOLUME_AS_ISSUE)],[issue()])
        observed=candidate(stem='Batman 001 - Book 9 (2020)')
        observed=replace(observed,filename=replace(observed.filename,volume_number=9))
        result=identify_authorized(observed,snapshot,1,PublicationAuthority.IMPORT_SELECTION)
        self.assertEqual(result.selected.local_issue_ids,(1,))
