"""Real neighboring services share the quarantine reservation boundary."""

import os
from contextlib import closing
from unittest import TestCase
from unittest.mock import patch
from zipfile import ZipFile

import TDuplicateQuarantine as fixture

from backend.base.bulk_folder import FolderReviewError
from backend.base.bulk_rename import RenameReviewError
from backend.base.duplicate_review import DuplicateReviewError
from backend.base.import_candidate import DiscoveryScope
from backend.base.library_health import HealthLevel
from backend.base.maintenance_review import Action
from backend.base.metadata_repair import RepairError
from backend.base.organization_job import OrganizationError
from backend.base.organization_plan import PlanningPolicy
from backend.features.bulk_folder import BulkFolderReviews
from backend.features.bulk_rename import BulkRenameReviews
from backend.features.comicinfo_repair import ComicInfoRepairReviews
from backend.features.local_artifact_planning import preview_local_artifacts
from backend.features.organization_execution import OrganizationExecutor


class QuarantineInteractionTests(TestCase):
    def case(self):
        case = fixture.DuplicateQuarantineTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def organization_review(self, case, folder=False):
        maintenance = case.fixture.fixture
        action, code = (Action.FOLDER, 'folder_deviation') if folder else (Action.RENAME, 'filename_deviation')
        work = maintenance.assign(maintenance.review(), action, code)
        service = BulkFolderReviews(maintenance.service) if folder else BulkRenameReviews(maintenance.service)
        review = service.create(case.db.cursor(), work.id, work.revision, work.manifest_digest,
            tuple(i.finding.id for i in work.items if i.selected))
        return service, review

    def register_other(self, case, service, review):
        return service.register(case.db.cursor(), review.id, review.revision, review.digest,
            confirmed=True, origin=review.origin, selected=review.selected)

    def test_real_rename_and_folder_jobs_exclude_both_directions(self):
        for folder in (False, True):
            for quarantine_first in (False, True):
                with self.subTest(folder=folder, quarantine_first=quarantine_first):
                    case = self.case()
                    case.db.execute("UPDATE config SET value='{series_name} ({year})' WHERE key='volume_folder_naming'")
                    case.db.commit()
                    duplicate = case.prepare()
                    other, review = self.organization_review(case, folder)
                    if quarantine_first:
                        case.register(duplicate)
                        with self.assertRaises((FolderReviewError, RenameReviewError, OrganizationError)):
                            self.register_other(case, other, review)
                    else:
                        registered = self.register_other(case, other, review)
                        with self.assertRaises(DuplicateReviewError):
                            case.register(duplicate)
                        self.assertEqual(other.execute(case.db.cursor(), registered['batch_id'])['state'], 'completed')
                        with self.assertRaises(DuplicateReviewError):
                            case.register(duplicate)
                    self.assertEqual(case.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone(), (1,))

    def test_real_comicinfo_registration_excludes_both_directions(self):
        for quarantine_first in (False, True):
            with self.subTest(quarantine_first=quarantine_first):
                case = self.case()
                paths = case.fixture.paths
                with ZipFile(paths[0], 'w') as archive:
                    archive.writestr('001.jpg', b'fixture page')
                paths[1].write_bytes(paths[0].read_bytes())
                case.db.execute('UPDATE files SET size=?', (paths[0].stat().st_size,))
                case.db.commit()
                duplicate = case.prepare()
                maintenance = case.fixture.fixture
                work = maintenance.assign(maintenance.review(HealthLevel.ARCHIVE), Action.COMICINFO, 'comicinfo_absent')
                comic = ComicInfoRepairReviews(maintenance.service)
                finding = next(i.finding.id for i in work.items if i.selected)
                review = comic.create(case.db.cursor(), work.id, work.revision, work.manifest_digest, finding)
                review = comic.revise(case.db.cursor(), review.id, 0, ('Series', 'Number', 'Provider identities'))
                def apply():
                    return comic.apply(case.db.cursor(), review.id, review.revision, review.digest,
                        confirmed=True, expected_authority=review.authority)
                if quarantine_first:
                    case.register(duplicate)
                    with self.assertRaises((RepairError, OrganizationError)):
                        apply()
                else:
                    with patch.object(OrganizationExecutor, 'apply_job', side_effect=SystemExit('registered only')):
                        with self.assertRaises(SystemExit):
                            apply()
                    with self.assertRaises(DuplicateReviewError):
                        case.register(duplicate)
                self.assertEqual(case.db.execute('SELECT COUNT(*) FROM organization_jobs').fetchone(), (1,))

    def test_real_local_intake_organization_excludes_both_directions(self):
        for quarantine_first in (False, True):
            with self.subTest(quarantine_first=quarantine_first):
                case = self.case()
                review = case.prepare()
                plan = preview_local_artifacts(case.db, (str(case.fixture.paths[0]),),
                    DiscoveryScope('quarantine-intake-fixture', str(case.health.root)),
                    PlanningPolicy(rename=True, windows=os.name == 'nt', case_sensitive=os.name != 'nt'), volume_id=1).plans[0]
                executor = OrganizationExecutor(str(case.health.database), (str(case.health.root),))
                self.addCleanup(executor.close)
                if quarantine_first:
                    case.register(review)
                    with self.assertRaises(OrganizationError):
                        executor.create_job(plan)
                else:
                    job = executor.create_job(plan)
                    with self.assertRaises(DuplicateReviewError):
                        case.register(review)
                    self.assertEqual(executor.apply_job(job).state.value, 'completed')
                self.assertEqual(case.db.execute('SELECT COUNT(*) FROM acquisition_downloads').fetchone(), (0,))

    def test_actual_metadata_repair_stales_duplicate_review_and_receipt_is_independent(self):
        import asyncio

        from TProviderSwitchApply import remote

        from backend.base.metadata_repair import Field, FieldSelection
        from backend.features.metadata_repair import MetadataRepairReviews
        from backend.implementations.metadata.switch_target import admit
        from backend.internals.metadata_repair_history import page
        case = self.case()
        duplicate = case.prepare()
        maintenance = case.fixture.fixture
        work = maintenance.assign(maintenance.review(), Action.METADATA)
        async def acquire(reference):
            return admit(remote(reference.provider, parent=reference.provider_id, first=101, count=1), reference)
        repairs = MetadataRepairReviews(maintenance.service, acquire=acquire, task_observer=lambda _: ())
        review = asyncio.run(repairs.create(case.db.cursor(), work.id, work.revision, work.manifest_digest,
            tuple(i.finding.id for i in work.items if i.selected)))
        review = repairs.revise(case.db.cursor(), review.id, 0, (FieldSelection('volume', 1, Field.TITLE),))
        self.assertEqual(repairs.apply(case.db.cursor(), review.id, review.revision, review.digest,
            confirmed=True, expected_authority=review.authority)['state'], 'applied')
        receipt = page(case.db.cursor(), 1)
        with self.assertRaises(DuplicateReviewError):
            case.register(duplicate)
        fresh = case.prepare()
        registered = case.register(fresh)
        self.assertEqual(case.service.execute(case.db.cursor(), registered['batch_id'])['state'], 'completed')
        self.assertEqual(page(case.db.cursor(), 1), receipt)

    def test_restored_file_returns_to_real_rename_folder_and_comicinfo_reviews(self):
        for kind in ('rename', 'folder', 'comicinfo'):
            with self.subTest(kind=kind):
                case = self.case()
                if kind == 'comicinfo':
                    with ZipFile(case.fixture.paths[0], 'w') as archive:
                        archive.writestr('001.jpg', b'fixture page')
                    case.fixture.paths[1].write_bytes(case.fixture.paths[0].read_bytes())
                    case.db.execute('UPDATE files SET size=?', (case.fixture.paths[0].stat().st_size,))
                case.db.execute("UPDATE config SET value='{series_name} ({year})' WHERE key='volume_folder_naming'")
                case.db.commit()
                duplicate = case.prepare()
                registered = case.register(duplicate)
                self.assertEqual(case.service.execute(case.db.cursor(), registered['batch_id'])['state'], 'completed')
                self.assertEqual(case.db.execute('SELECT id FROM active_files WHERE id=1').fetchall(), [])
                with closing(OrganizationExecutor(str(case.health.database), (str(case.health.root),))) as executor:
                    job = registered['jobs'][0]['id']
                    preview = executor.preview_undo(job)
                    self.assertTrue(preview.eligible, preview.reasons)
                    inverse = executor.create_undo_job(job, preview.intent_digest)
                    self.assertEqual(executor.apply_job(inverse).state.value, 'completed')
                self.assertEqual(case.db.execute('SELECT id FROM active_files WHERE id=1').fetchall(), [(1,)])
                if kind == 'comicinfo':
                    maintenance = case.fixture.fixture
                    work = maintenance.assign(maintenance.review(HealthLevel.ARCHIVE), Action.COMICINFO, 'comicinfo_absent')
                    comic = ComicInfoRepairReviews(maintenance.service)
                    finding = next(i.finding.id for i in work.items if i.selected)
                    review = comic.create(case.db.cursor(), work.id, work.revision, work.manifest_digest, finding)
                    review = comic.revise(case.db.cursor(), review.id, 0, ('Series', 'Number', 'Provider identities'))
                    result = comic.apply(case.db.cursor(), review.id, review.revision, review.digest,
                                         confirmed=True, expected_authority=review.authority)
                    self.assertEqual(result['state'], 'completed')
                else:
                    service, review = self.organization_review(case, kind == 'folder')
                    batch = self.register_other(case, service, review)
                    self.assertEqual(service.execute(case.db.cursor(), batch['batch_id'])['state'], 'completed')
