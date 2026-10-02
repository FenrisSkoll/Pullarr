"""Additive schema 72: lifecycle attached to existing acquisition/intake jobs."""

from backend.internals.quality_schema import STATEMENTS as QUALITY_STATEMENTS

_PROVENANCE = next(s for s in QUALITY_STATEMENTS if s.startswith('CREATE TABLE acquisition_provenance '))
_UPGRADE_VIEW = next(s for s in QUALITY_STATEMENTS if s.startswith('CREATE VIEW quality_upgrade_issues '))
_REBUILD = (
    'DROP VIEW quality_upgrade_issues',
    _PROVENANCE.replace('acquisition_provenance', '_expanded_acquisition_provenance').replace(
        "('sabnzbd','direct_download','manual')", "('sabnzbd','nzbget','qbittorrent','direct_download','manual')"),
    'INSERT INTO _expanded_acquisition_provenance SELECT * FROM acquisition_provenance',
    # Self-referential RESTRICT must be cleared only after an exact transactional
    # copy. No outside table references this domain; lineage is preserved above.
    'UPDATE acquisition_provenance SET supersedes=NULL',
    'DROP TABLE acquisition_provenance',
    'ALTER TABLE _expanded_acquisition_provenance RENAME TO acquisition_provenance',
    *(s for s in QUALITY_STATEMENTS if s.startswith('CREATE INDEX quality_provenance_')),
    _UPGRADE_VIEW,
)

STATEMENTS = _REBUILD + (
    '''CREATE TABLE IF NOT EXISTS acquisition_torrents(
        download_id TEXT PRIMARY KEY REFERENCES acquisition_downloads(id),
        infohash_v1 TEXT CHECK(infohash_v1 IS NULL OR length(infohash_v1)=40),
        infohash_v2 TEXT CHECK(infohash_v2 IS NULL OR length(infohash_v2)=64),
        policy TEXT NOT NULL CHECK(json_valid(policy)),
        requirements TEXT NOT NULL CHECK(json_valid(requirements)),
        state TEXT NOT NULL DEFAULT 'submitted'
            CHECK(state IN ('submitted','downloading','seeding','retained','review','removed_keep','removed_data')),
        observation TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(observation)),
        observed_at REAL,
        cleanup_revision INTEGER NOT NULL DEFAULT 1 CHECK(cleanup_revision>0),
        cleanup_receipt TEXT CHECK(cleanup_receipt IS NULL OR json_valid(cleanup_receipt)),
        CHECK(infohash_v1 IS NOT NULL OR infohash_v2 IS NOT NULL)
    )''',
    '''CREATE INDEX IF NOT EXISTS acquisition_torrents_v1 ON acquisition_torrents(infohash_v1)''',
    '''CREATE INDEX IF NOT EXISTS acquisition_torrents_v2 ON acquisition_torrents(infohash_v2)''',
    '''CREATE INDEX IF NOT EXISTS acquisition_torrents_poll ON acquisition_torrents(state,observed_at)''',
    '''CREATE TABLE IF NOT EXISTS acquisition_seed_artifacts(
        intake_id TEXT NOT NULL REFERENCES acquisition_intakes(id),
        source_path TEXT NOT NULL,
        staged_path TEXT NOT NULL UNIQUE,
        copy_job_id TEXT NOT NULL UNIQUE REFERENCES organization_jobs(id),
        source_identity TEXT NOT NULL CHECK(json_valid(source_identity)),
        import_method TEXT CHECK(import_method IN ('hardlink','copy')),
        PRIMARY KEY(intake_id,source_path)
    )''',
)

SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
