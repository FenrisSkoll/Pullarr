"""Characterize current title semantics, never implement a proposed display rule."""

from pathlib import Path
from unittest import TestCase
from unittest.mock import AsyncMock, patch

from fixtures.collected_titles import CORPUS, cv_issue_snapshot, record
from fixtures.comicvine_fetch import issue_response
from fixtures.comicvine_search import volume_response
from fixtures.metadata_refresh import RefreshHarness
from Tbackend.implementations.metron_lifecycle import MetronHarness

from backend.base.custom_exceptions import KeyNotFound
from backend.base.definitions import DateType, SpecialVersion
from backend.implementations.comicvine import ComicVine
from backend.implementations.metadata.comicvine import \
    ComicVineMetadataProvider
from backend.implementations.metadata.enrichment import VolumeFetchResult
from backend.implementations.metadata.metron import MetronMetadataProvider
from backend.implementations.metadata.persistence import ProviderVolumeIdentity
from backend.implementations.naming import (generate_issue_name,
                                            get_issue_naming_keys)
from backend.implementations.volumes import Library, Volume, refresh_and_scan
from frontend.metadata import issue_identity_result, volume_identity_results


def cv_mapped(volume, issues):
    client = ComicVine()
    result = client._ComicVine__format_volume_output(volume)
    result['issues'] = [client._ComicVine__format_issue_output(row) for row in issues]
    return ComicVineMetadataProvider._volume_metadata(result)


class CollectedTitles(MetronHarness, TestCase):
    def persist(self, metadata):
        # No live fetch or cover download. Real schema, identity writes, title
        # writes, special-version calculation, public serialization and naming.
        with patch('backend.implementations.volumes.fetch_volume_result',
                   new=AsyncMock(return_value=VolumeFetchResult(metadata, ()))):
            local = Library.add_metadata(ProviderVolumeIdentity(metadata.provider, metadata.provider_id), 1, True)
        return Volume(local)

    def test_cyberpunk_real_tpb_through_mapping_storage_ui_payload_and_naming(self):
        raw = record('cyberpunk-cv-collected-issue')
        self.assertEqual((raw['id'], raw['issue_number'], raw['name']), (987246, '1', 'TPB'))
        metadata = cv_mapped(record('cyberpunk-cv-collected-volume'), [raw])
        volume = self.persist(metadata)
        data, issue = volume.get_data(), volume.get_issues()[0]
        self.assertEqual(data.title, 'Cyberpunk 2077: You Have My Word')
        self.assertEqual(issue.title, 'TPB')
        self.assertEqual(data.special_version, SpecialVersion.TPB)
        public = volume_identity_results([volume.get_public_data()], True)[0]
        self.assertEqual(public['issues'][0]['title'], 'TPB')
        # Phase 2D-B adds presentation only to opt-in output; legacy stays raw.
        legacy = volume_identity_results([volume.get_public_data()], False)[0]
        self.assertNotIn('display_title', legacy['issues'][0])
        self.assertEqual(issue_identity_result(issue, True)['title'], 'TPB')
        self.assertEqual(get_issue_naming_keys(data, issue).issue_title, 'TPB')
        self.assertEqual(generate_issue_name(data, 1.0),
                         'Cyberpunk 2077 - You Have My Word (2023) Volume 01 TPB')

    def test_cyberpunk_floppy_null_title_and_normal_classification(self):
        issues = cv_issue_snapshot('cyberpunk-cv-floppy-volume', ['cyberpunk-cv-floppy-issue'])
        volume = self.persist(cv_mapped(record('cyberpunk-cv-floppy-volume'), issues))
        self.assertIsNone(volume.get_issues()[0].title)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.NORMAL)
        self.assertEqual(volume.get_data().title, 'Cyberpunk 2077: You Have My Word')

    def test_normal_floppy_titled_issue_is_not_series_title(self):
        issues = cv_issue_snapshot('halloween-floppy-cv-volume', ['halloween-floppy-cv-issue'])
        volume = self.persist(cv_mapped(record('halloween-floppy-cv-volume'), issues))
        first = next(i for i in volume.get_issues() if i.issue_number == '1')
        self.assertEqual(first.title, 'Crime')
        self.assertNotEqual(volume.get_data().title, 'Crime')
        self.assertEqual(volume.get_data().special_version, SpecialVersion.NORMAL)
        metron = MetronMetadataProvider.issue_metadata(record('halloween-floppy-metron-issue'), '1029', DateType.COVER_DATE)
        self.assertEqual(metron.title, 'Crime')

    def test_cv_dedicated_trade_volume_vai_and_distinct_ordinal_titles(self):
        issues = cv_issue_snapshot('saga-trades-cv-volume', ['saga-trade1-cv-issue', 'saga-trade2-cv-issue'])
        volume = self.persist(cv_mapped(record('saga-trades-cv-volume'), issues))
        self.assertEqual(volume.get_data().special_version, SpecialVersion.VOLUME_AS_ISSUE)
        by_number = {i.issue_number: i.title for i in volume.get_issues()}
        self.assertEqual([by_number['1'], by_number['2']], ['Volume One', 'Volume Two'])
        self.assertEqual(generate_issue_name(volume.get_data(), (1.0, 2.0)), 'Saga (2012) Volume 001 - 002')

    def test_metron_collection_title_takes_precedence_over_story_titles(self):
        for number, label in ((1, 'One'), (2, 'Two')):
            raw = record('saga-trade%d-metron-issue' % number)
            issue = MetronMetadataProvider.issue_metadata(raw, '3892', DateType.COVER_DATE)
            self.assertEqual(issue.title, 'Volume ' + label)
            self.assertTrue(raw['name'])
            self.assertNotEqual(issue.title, '; '.join(raw['name']))
            self.assertNotIn('story_titles', vars(issue))

    def test_hardcover_same_issue_different_title_and_classification(self):
        cv_raw = record('halloween-hc-cv-issue')
        metron_raw = record('halloween-hc-metron-issue')
        self.assertEqual(metron_raw['cv_id'], cv_raw['id'])
        cv = self.persist(cv_mapped(record('halloween-hc-cv-volume'), [cv_raw]))
        metadata = MetronMetadataProvider.volume_result(record('halloween-hc-metron-series'),
                                                       [metron_raw], DateType.COVER_DATE).metadata
        metron = self.persist(metadata)
        self.assertEqual(cv.get_issues()[0].title, 'HC')
        self.assertEqual(cv.get_data().special_version, SpecialVersion.HARD_COVER)
        self.assertEqual(metron.get_issues()[0].title, '; '.join(metron_raw['name']))
        self.assertEqual(len(metron_raw['name']), 13)
        # Current adapter does not carry the provider's series_type into detection.
        self.assertEqual(metron.get_data().special_version, SpecialVersion.TPB)

    def test_metron_untitled_collection_stays_null_not_series_fallback(self):
        raw = record('kickdown-metron-issue')
        result = MetronMetadataProvider.volume_result(record('kickdown-metron-series'), [raw], DateType.COVER_DATE)
        volume = self.persist(result.metadata)
        self.assertIsNone(volume.get_issues()[0].title)
        self.assertEqual(volume.get_data().special_version, SpecialVersion.TPB)
        self.assertEqual(volume.get_data().title, 'Cyberpunk 2077: Kickdown')

    def test_equivalence_uses_explicit_issue_references_only(self):
        for prefix in ('halloween-floppy', 'halloween-hc', 'saga-trade1', 'saga-trade2'):
            self.assertEqual(record(prefix + '-metron-issue')['cv_id'], record(prefix + '-cv-issue')['id'])
        self.assertIsNone(record('saga-trades-metron-series')['cv_id'])
        # Issue equivalence must not invent a series-level reference.
        self.assertNotEqual(record('cyberpunk-cv-collected-volume')['id'], record('cyberpunk-cv-floppy-volume')['id'])

    def test_hostile_colon_titles_are_never_split(self):
        for name in ('Batman: Detective Comics', 'Star Wars: Darth Vader', 'Cyberpunk 2077: You Have My Word'):
            metadata = cv_mapped(volume_response(name=name), [issue_response(name='TPB')])
            self.assertEqual(metadata.title, name)
            self.assertEqual(metadata.issues[0].title, 'TPB')
            self.assertNotEqual(metadata.title, name.split(':')[-1].strip())

    def test_synthetic_one_shot_omnibus_hardcover_vai_detection_no_retitle(self):
        for index, (name, title, special) in enumerate((
            ('Synthetic One-Shot', 'A meaningful story', SpecialVersion.ONE_SHOT),
            ('Synthetic Omnibus', 'Omnibus', SpecialVersion.OMNIBUS),
            ('Synthetic Hardcover', 'HC', SpecialVersion.HARD_COVER),
            ('Synthetic Trades', 'Volume 2: A Beginning', SpecialVersion.VOLUME_AS_ISSUE)
        )):
            remote = 800000 + index
            metadata = cv_mapped(volume_response(id=remote, name=name, count_of_issues=1),
                                 [issue_response(id=900000 + index, volume={'id': remote}, name=title)])
            volume = self.persist(metadata)
            self.assertEqual(volume.get_issues()[0].title, title)
            self.assertEqual(volume.get_data().special_version, special)

    def test_empty_title_remains_none(self):
        for title in ('', None):
            metadata = cv_mapped(volume_response(), [issue_response(name=title)])
            self.assertIsNone(metadata.issues[0].title)

    def test_public_title_override_not_supported_but_format_lock_is(self):
        volume = self.persist(cv_mapped(record('cyberpunk-cv-collected-volume'), [record('cyberpunk-cv-collected-issue')]))
        with self.assertRaises(KeyNotFound):
            volume.update({'title': 'Manual'}, from_public=True)
        volume.update({'special_version': SpecialVersion.HARD_COVER, 'special_version_locked': True}, from_public=True)
        self.assertTrue(volume.get_data().special_version_locked)
        self.assertEqual(volume.get_issues()[0].title, 'TPB')

    def test_current_ui_reads_stored_title_not_display_title(self):
        source = (Path(__file__).resolve().parents[3] / 'frontend/static/js/view_volume.js')
        # Docker tests are mounted under /app/tests, same repository layout.
        text = source.read_text(encoding='utf-8')
        self.assertIn('inst.title.innerText = obj.title;', text)
        self.assertIn('ViewEls.vol_data.title.innerText = data.title;', text)

    def test_fixture_inventory_is_metadata_only(self):
        self.assertEqual(len(CORPUS['records']), 46)
        for value in CORPUS['records'].values():
            self.assertIn(value['provider'], ('comicvine', 'metron'))
            self.assertNotIn('Authorization', value['data'])
            self.assertNotIn('account_id', value['data'])

    def test_second_real_hc_and_normal_control(self):
        hc = cv_mapped(record('overture-deluxe-cv-volume'), [record('overture-deluxe-cv-issue')])
        volume = self.persist(hc)
        self.assertEqual(volume.get_issues()[0].title, 'HC')
        self.assertEqual(volume.get_data().special_version, SpecialVersion.HARD_COVER)
        normal = cv_mapped(record('halloween-floppy-cv-volume'), [record('halloween-floppy2-cv-issue')])
        self.assertEqual(normal.issues[0].title, 'Thanksgiving')

    def test_deluxe_number_is_not_bare_title(self):
        for label, expected in (
            ('batman-deluxe', 'Book 1'), ('sandman-deluxe', 'Book One'),
            ('dmz-deluxe', 'Book One'), ('hack-deluxe', 'Volume 1'),
            ('detective-deluxe', 'Book 1'), ('descender', 'Volume One'),
            ('revival', 'Volume One'), ('invincible', 'Volume 1'),
            ('transformers', 'Compendium One'), ('bprd', 'Volume 1')
        ):
            with self.subTest(label=label):
                raw = record(label + '-cv-volume')
                self.assertEqual(raw['issues'][0]['name'], expected)
                mapped = cv_mapped(raw, cv_issue_snapshot(label + '-cv-volume', []))
                self.assertEqual(mapped.issues[0].title, expected)
                self.assertNotEqual(mapped.issues[0].title, mapped.issues[0].issue_number)

    def test_gideon_meaningful_book_titles_survive_without_punctuation_guess(self):
        raw_issues = [record('gideon1-cv-issue'), record('gideon2-cv-issue')]
        volume = self.persist(cv_mapped(record('gideon-cv-volume'), raw_issues))
        titles = {i.issue_number: i.title for i in volume.get_issues()}
        self.assertEqual(titles, {'1': 'Book 1. The Legend of the Black Barn',
                                  '2': 'Book 2. The Eater of All Things'})

    def test_batman_six_books_are_two_provider_volumes(self):
        first, second = record('batman-deluxe-cv-volume'), record('batman-continuation-cv-volume')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual([i['issue_number'] for i in first['issues']], ['1', '2', '3'])
        self.assertEqual([i['issue_number'] for i in second['issues']], ['4', '5', '6'])

    def test_adventure_null_collection_title_is_not_invented(self):
        metadata = cv_mapped(record('adventure-cv-volume'),
                             cv_issue_snapshot('adventure-cv-volume', ['adventure1-cv-issue']))
        self.assertTrue(all(i.title is None for i in metadata.issues))
        self.assertTrue(all(i['name'] is None for i in record('adventure-cv-volume')['issues']))

    def test_compendium_cross_reference_proves_different_raw_titles(self):
        raw = record('invincible1-metron-issue')
        cv_issue = record('invincible-cv-volume')['issues'][0]
        self.assertEqual(raw['cv_id'], cv_issue['id'])
        self.assertEqual(cv_issue['name'], 'Volume 1')
        result = MetronMetadataProvider.volume_result(record('invincible-metron-series'),
            [record('invincible%d-metron-issue' % i) for i in (1, 2, 3)], DateType.COVER_DATE)
        self.assertEqual([i.title for i in result.metadata.issues],
                         ['Compendium One', 'Compendium Two', 'Compendium Three'])
        self.assertTrue(all(not record('invincible%d-metron-issue' % i)['name'] for i in (1, 2, 3)))

    def test_deluxe_classifications_are_independent_of_real_binding(self):
        for label, expected in (
            ('batman-deluxe', SpecialVersion.NORMAL),
            ('batman-continuation', SpecialVersion.NORMAL),
            ('sandman-deluxe', SpecialVersion.NORMAL),
            ('dmz-deluxe', SpecialVersion.NORMAL),
            ('detective-deluxe', SpecialVersion.NORMAL),
            ('hack-deluxe', SpecialVersion.NORMAL),
            ('gideon', SpecialVersion.NORMAL),
            ('transformers', SpecialVersion.NORMAL),
            ('adventure', SpecialVersion.NORMAL),
            ('invincible', SpecialVersion.VOLUME_AS_ISSUE),
            ('descender', SpecialVersion.VOLUME_AS_ISSUE),
            ('revival', SpecialVersion.VOLUME_AS_ISSUE),
            ('bprd', SpecialVersion.VOLUME_AS_ISSUE),
            ('rai', SpecialVersion.HARD_COVER),
        ):
            with self.subTest(label=label):
                key = label + '-cv-volume'
                metadata = cv_mapped(record(key), cv_issue_snapshot(key, []))
                volume = self.persist(metadata)
                self.assertEqual(volume.get_data().special_version, expected)

    def test_bare_number_is_synthetic_not_a_claim_about_live_corpus(self):
        for title in ('1', '2', 'Hardcover', 'Trade Paperback', 'Vol. 1'):
            mapped = cv_mapped(volume_response(), [issue_response(name=title)])
            self.assertEqual(mapped.issues[0].title, title)
        observed = [i['name'] for item in CORPUS['records'].values()
                    for i in item['data'].get('issues', [])]
        self.assertFalse(any(name and name.isdecimal() for name in observed))

    def test_metron_refresh_replaces_manual_titles_without_renaming_folder(self):
        result = MetronMetadataProvider.volume_result(record('kickdown-metron-series'),
                                                      [record('kickdown-metron-issue')], DateType.COVER_DATE)
        volume = self.persist(result.metadata)
        folder = volume.get_data().folder
        volume.update({'title': 'Direct internal edit'})
        self.db.execute('UPDATE issues SET title=? WHERE volume_id=?', ('Manual title', volume.id))
        self.db.commit()
        with patch.object(MetronMetadataProvider, 'fetch_volume_enriched', new=AsyncMock(return_value=result)):
            refresh_and_scan(volume.id)
        self.assertIsNone(volume.get_issues()[0].title)
        self.assertEqual(volume.get_data().title, result.metadata.title)
        self.assertEqual(volume.get_data().folder, folder)


class CVCollectedRefresh(RefreshHarness, TestCase):
    def test_real_tpb_refresh_restores_provider_title_and_preserves_format_lock(self):
        volume_raw, issue_raw = record('cyberpunk-cv-collected-volume'), record('cyberpunk-cv-collected-issue')
        self.prepare_fetch(volume_raw, [issue_raw])
        local = Library.add(volume_raw['id'], 1, True)
        volume = Volume(local)
        folder = volume.get_data().folder
        volume.update({'title': 'Manual', 'special_version': SpecialVersion.HARD_COVER, 'special_version_locked': True})
        self.db.execute('UPDATE issues SET title=? WHERE volume_id=?', ('Manual subtitle', local))
        self.db.commit()
        self.prepare_refresh([volume_raw], [issue_raw])
        refresh_and_scan(local, allow_skipping=False)
        self.assertEqual(volume.get_issues()[0].title, 'TPB')
        self.assertEqual(volume.get_data().title, volume_raw['name'])
        self.assertEqual(volume.get_data().special_version, SpecialVersion.HARD_COVER)
        self.assertEqual(volume.get_data().folder, folder)
