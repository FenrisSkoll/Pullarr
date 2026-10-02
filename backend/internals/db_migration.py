# -*- coding: utf-8 -*-

from asyncio import run
from typing import Callable, Dict, List

from backend.base.definitions import DownloadType, GCDownloadService
from backend.base.helpers import CommaList
from backend.base.logging import LOGGER
from backend.internals.db import get_db, iter_commit


# region Handler
class DatabaseMigrationHandler:
    """Handles the registration of all migrators and running them if needed.
    To add a migration, simply write the funtion and decorate it with
    `register_handler(...)`. The `migrate(...)` method will take care of running
    it.
    """

    handlers: Dict[int, Callable[[], None]] = {}

    @classmethod
    def register_handler(cls, start_version: int):
        """Register a database migrator.

        Args:
            start_version (int): The database version that it migrates _from_.
                So start_version=2 means migrating from 2 to 3.

        Raises:
            RuntimeError: A database migration with the given start_version is
                already registered.
        """
        def wrapper(migrator: Callable[[], None]):
            if start_version in cls.handlers:
                raise RuntimeError(
                    f"Database migration with start version {start_version} "
                    "registered multiple times"
                )
            cls.handlers[start_version] = migrator
            return migrator
        return wrapper

    @classmethod
    def latest_db_version(cls) -> int:
        """Get the latest database version supported.

        Returns:
            int: The version.
        """
        return max(cls.handlers) + 1

    @classmethod
    def is_first_startup(cls) -> bool:
        """Whether this is the first startup ever. Specifically, whether the
        config table is present in the database.

        Returns:
            bool: Whether this is the first startup ever.
        """
        result = get_db().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='config';"
        ).exists() is None
        return result

    @classmethod
    def on_first_startup(cls) -> None:
        """Handle the first startup ever"""
        cursor = get_db()

        # Insert GetComics indexer
        cursor.execute("""
            INSERT INTO indexer_clients(
                enabled,
                download_type, client_type,
                title, url,
                gc_service_preference,
                gc_avoid_large_downloads
            ) VALUES (
                :enabled,
                :download_type, :client_type,
                :title, :url,
                :gc_service_preference,
                :gc_avoid_large_downloads
            );
            """,
            {
                "enabled": True,
                "download_type": DownloadType.DDL,
                "client_type": "GetComics",
                "title": "GetComics",
                "url": "https://getcomics.org",
                "gc_service_preference": str(CommaList(
                    (s.value for s in GCDownloadService._member_map_.values())
                )),
                "gc_avoid_large_downloads": False
            }
        )
        return

    @classmethod
    def migrate(cls) -> None:
        """
        Migrate a Kapowarr database from its current version to the newest
        version supported by the Kapowarr version installed.
        """
        from backend.internals.settings import Settings

        s = Settings()
        current_db_version = s.sv.database_version
        newest_version = cls.latest_db_version()

        if current_db_version > newest_version:
            LOGGER.warning(
                "Database is for newer version of Kapowarr"
            )
            return

        if current_db_version == newest_version:
            return

        LOGGER.info("Migrating database to newer version...")
        LOGGER.debug(
            "Database migration: %d -> %d",
            current_db_version, newest_version
        )

        for start_version in iter_commit(
            range(current_db_version, newest_version)
        ):
            if start_version not in cls.handlers:
                continue

            cls.handlers[start_version]()
            s.update({"database_version": start_version + 1})

        get_db().execute("VACUUM;")
        s.clear_cache()

        return


# region Migrators
# Please name all of the migrators with an underscore prefix. This way they
# won't show up as importable functions in other files by IDEs.

@DatabaseMigrationHandler.register_handler(1)
def _migrate_clear_download_queue():
    get_db().executescript("DELETE FROM download_queue;")
    return


@DatabaseMigrationHandler.register_handler(2)
def _migrate_update_issues_and_files():
    get_db().executescript("""
        BEGIN TRANSACTION;
        PRAGMA defer_foreign_keys = ON;

        -- Issues
        CREATE TEMPORARY TABLE temp_issues_3 AS
            SELECT * FROM issues;
        DROP TABLE issues;

        CREATE TABLE issues(
            id INTEGER PRIMARY KEY,
            volume_id INTEGER NOT NULL,
            comicvine_id INTEGER NOT NULL,
            issue_number VARCHAR(20) NOT NULL,
            calculated_issue_number FLOAT(20) NOT NULL,
            title VARCHAR(255),
            date VARCHAR(10),
            description TEXT,
            monitored BOOL NOT NULL DEFAULT 1,

            FOREIGN KEY (volume_id) REFERENCES volumes(id)
                ON DELETE CASCADE
        );
        INSERT INTO issues
            SELECT * FROM temp_issues_3;

        -- Issues files
        CREATE TEMPORARY TABLE temp_issues_files_3 AS
            SELECT * FROM issues_files;
        DROP TABLE issues_files;

        CREATE TABLE issues_files(
            file_id INTEGER NOT NULL,
            issue_id INTEGER NOT NULL,

            FOREIGN KEY (file_id) REFERENCES files(id)
                ON DELETE CASCADE,
            FOREIGN KEY (issue_id) REFERENCES issues(id),
            CONSTRAINT PK_issues_files PRIMARY KEY (
                file_id,
                issue_id
            )
        );
        INSERT INTO issues_files
            SELECT * FROM temp_issues_files_3;

        COMMIT;
    """)
    return


@DatabaseMigrationHandler.register_handler(3)
def _migrate_remove_unmatched_files():
    from backend.internals.db_models import FilesDB

    FilesDB.delete_unmatched_files()

    return


@DatabaseMigrationHandler.register_handler(4)
def _migrate_recalculate_issue_number():
    from backend.base.file_extraction import extract_issue_number

    cursor = get_db()
    iter_cursor = get_db(force_new=True)
    iter_cursor.execute("SELECT id, issue_number FROM issues;")
    for result in iter_cursor:
        calc_issue_number = extract_issue_number(result[1])
        cursor.execute(
            "UPDATE issues SET calculated_issue_number = ? WHERE id = ?;",
            (calc_issue_number, result[0])
        )
    return


@DatabaseMigrationHandler.register_handler(5)
def _migrate_add_cv_fetch_time():
    from backend.implementations.comicvine import ComicVine

    cursor = get_db()
    cursor.executescript("""
        BEGIN TRANSACTION;
        PRAGMA defer_foreign_keys = ON;

        -- Issues
        CREATE TEMPORARY TABLE temp_issues_6 AS
            SELECT * FROM issues;
        DROP TABLE issues;

        CREATE TABLE IF NOT EXISTS issues(
            id INTEGER PRIMARY KEY,
            volume_id INTEGER NOT NULL,
            comicvine_id INTEGER UNIQUE NOT NULL,
            issue_number VARCHAR(20) NOT NULL,
            calculated_issue_number FLOAT(20) NOT NULL,
            title VARCHAR(255),
            date VARCHAR(10),
            description TEXT,
            monitored BOOL NOT NULL DEFAULT 1,

            FOREIGN KEY (volume_id) REFERENCES volumes(id)
                ON DELETE CASCADE
        );
        INSERT INTO issues
            SELECT * FROM temp_issues_6;

        -- Volumes
        ALTER TABLE volumes
            ADD last_cv_update VARCHAR(255);
        ALTER TABLE volumes
            ADD last_cv_fetch INTEGER(8) DEFAULT 0;

        COMMIT;
    """)

    volume_ids = [
        str(v[0])
        for v in cursor.execute("SELECT comicvine_id FROM volumes;")
    ]
    updates = (
        ('', r['comicvine_id'])
        for r in run(ComicVine().fetch_volumes(volume_ids))
    )
    cursor.executemany(
        "UPDATE volumes SET last_cv_update = ? WHERE comicvine_id = ?;",
        updates
    )

    return


@DatabaseMigrationHandler.register_handler(6)
def _migrate_add_custom_folder():
    get_db().execute("""
        ALTER TABLE volumes
            ADD custom_folder BOOL NOT NULL DEFAULT 0;
    """)
    return


@DatabaseMigrationHandler.register_handler(7)
def _migrate_add_special_version():
    from backend.implementations.volumes import (Library,
                                                 determine_special_version)

    cursor = get_db()
    cursor.execute("""
        ALTER TABLE volumes
            ADD special_version VARCHAR(255);
    """)

    updates = (
        (
            determine_special_version(v_id),
            v_id
        )
        for v_id in Library.get_volumes()
    )

    cursor.executemany(
        "UPDATE volumes SET special_version = ? WHERE id = ?;",
        updates
    )
    return


@DatabaseMigrationHandler.register_handler(8)
def _migrate_update_volume_table():
    get_db().executescript("""
        PRAGMA foreign_keys = OFF;

        CREATE TABLE new_volumes(
            id INTEGER PRIMARY KEY,
            comicvine_id INTEGER NOT NULL,
            title VARCHAR(255) NOT NULL,
            year INTEGER(5),
            publisher VARCHAR(255),
            volume_number INTEGER(8) DEFAULT 1,
            description TEXT,
            cover BLOB,
            monitored BOOL NOT NULL DEFAULT 0,
            root_folder INTEGER NOT NULL,
            folder TEXT,
            custom_folder BOOL NOT NULL DEFAULT 0,
            last_cv_fetch INTEGER(8) DEFAULT 0,
            special_version VARCHAR(255),

            FOREIGN KEY (root_folder) REFERENCES root_folders(id)
        );

        INSERT INTO new_volumes
            SELECT
                id, comicvine_id, title, year, publisher,
                volume_number, description, cover, monitored,
                root_folder, folder, custom_folder,
                0 AS last_cv_fetch, special_version
            FROM volumes;

        DROP TABLE volumes;

        ALTER TABLE new_volumes RENAME TO volumes;

        PRAGMA foreign_keys = ON;
    """)
    return


@DatabaseMigrationHandler.register_handler(9)
def _migrate_update_manifest():

    # There used to be a migration here that fixed the manifest file.
    # That has since been replaced by the dynamic endpoint serving the JSON.
    # So the migration doesn't do anything anymore, and a function used
    # doesn't exist anymore, so the whole migration is just removed.

    return


@DatabaseMigrationHandler.register_handler(10)
def _migrate_update_special_version():
    from backend.implementations.volumes import (Library,
                                                 determine_special_version)

    updates = (
        (
            determine_special_version(v_id),
            v_id
        )
        for v_id in Library.get_volumes()
    )

    get_db().executemany(
        "UPDATE volumes SET special_version = ? WHERE id = ?;",
        updates
    )

    return


@DatabaseMigrationHandler.register_handler(11)
def _migrate_add_torrent_client_to_download_queue():
    get_db().executescript("""
        DROP TABLE download_queue;

        CREATE TABLE download_queue(
            id INTEGER PRIMARY KEY,
            client_type VARCHAR(255) NOT NULL,
            torrent_client_id INTEGER,

            link TEXT NOT NULL,
            filename_body TEXT NOT NULL,
            source VARCHAR(25) NOT NULL,

            volume_id INTEGER NOT NULL,
            issue_id INTEGER,
            page_link TEXT,

            FOREIGN KEY (torrent_client_id) REFERENCES torrent_clients(id),
            FOREIGN KEY (volume_id) REFERENCES volumes(id),
            FOREIGN KEY (issue_id) REFERENCES issues(id)
        );
    """)
    return


@DatabaseMigrationHandler.register_handler(12)
def _migrate_unzip_to_format_preference():
    cursor = get_db()
    unzip = cursor.execute(
        "SELECT value FROM config WHERE key = 'unzip' LIMIT 1;"
    ).fetchone()[0]

    cursor.execute(
        "DELETE FROM config WHERE key = 'unzip';"
    )

    if unzip:
        cursor.executescript("""
            UPDATE config
            SET value = 'folder'
            WHERE key = 'format_preference';

            UPDATE config
            SET value = 1
            WHERE key = 'convert';
            """
        )
    return


@DatabaseMigrationHandler.register_handler(13)
def _migrate_folder_conversion_to_own_setting():
    cursor = get_db()
    format_preference: List[str] = cursor.execute("""
        SELECT value
        FROM config
        WHERE key = 'format_preference'
        LIMIT 1;
    """).fetchone()[0].split(',')

    if 'folder' in format_preference:
        cursor.execute("""
            UPDATE config
            SET value = 1
            WHERE key = 'extract_issue_ranges';
            """
        )
        format_preference.remove('folder')
        cursor.execute("""
            UPDATE config
            SET value = ?
            WHERE key = 'format_preference';
            """,
            (",".join(format_preference),)
        )
    return


@DatabaseMigrationHandler.register_handler(14)
def _migrate_service_preference_to_setting():
    cursor = get_db()
    service_preference = ','.join([
        source[0] for source in cursor.execute(
            "SELECT source FROM service_preference ORDER BY pref;"
        )
    ])

    # UPDATE, not INSERT,
    # because first default settings are entered and only then is the db
    # migration done, so the key will already exist.
    cursor.execute(
        "UPDATE config SET value = ? WHERE key = 'service_preference';",
        (service_preference,)
    )
    cursor.execute(
        "DROP TABLE service_preference;"
    )
    return


@DatabaseMigrationHandler.register_handler(15)
def _migrate_update_blocklist_table():
    get_db().executescript("""
        BEGIN TRANSACTION;
        PRAGMA defer_foreign_keys = ON;

        DROP TABLE blocklist_reasons;

        CREATE TEMPORARY TABLE temp_blocklist_16 AS
            SELECT * FROM blocklist;
        DROP TABLE blocklist;

        CREATE TABLE blocklist(
            id INTEGER PRIMARY KEY,
            link TEXT NOT NULL UNIQUE,
            reason INTEGER NOT NULL CHECK (reason > 0),
            added_at INTEGER NOT NULL
        );
        INSERT INTO blocklist
            SELECT * FROM temp_blocklist_16;

        COMMIT;
    """)
    return


@DatabaseMigrationHandler.register_handler(16)
def _migrate_log_level_to_int():
    from backend.internals.settings import Settings

    s = Settings()
    log_number = 20 if s.sv.log_level == 'info' else 10
    s.update({"log_level": log_number})

    return


@DatabaseMigrationHandler.register_handler(17)
def _migrate_add_special_version_lock():
    get_db().execute("""
        ALTER TABLE volumes ADD
            special_version_locked BOOL NOT NULL DEFAULT 0
    """)
    return


@DatabaseMigrationHandler.register_handler(18)
def _migrate_tpb_naming_to_special_version_naming():
    from re import IGNORECASE, compile

    cursor = get_db()

    format: str = cursor.execute(
        "SELECT value FROM config WHERE key = 'file_naming_tpb' LIMIT 1;"
    ).fetchone()[0]

    cursor.execute(
        "DELETE FROM config WHERE key = 'file_naming_tpb';"
    )

    tpb_replacer = compile(
        r'\b(tpb|trade[\s\.\-]?paper[\s\.\-]?back)\b',
        IGNORECASE
    )
    format = tpb_replacer.sub('{special_version}', format)

    cursor.execute(
        "UPDATE config SET value = ? WHERE key = 'file_naming_special_version';",
        (format,))

    return


@DatabaseMigrationHandler.register_handler(19)
def _migrate_add_we_transfer_to_preference():
    # This migration used to add WeTransfer to the service preference list.
    # This setting doesn't exist anymore, as it was moved to the indexer
    # setting.
    return


@DatabaseMigrationHandler.register_handler(20)
def _migrate_clear_unsupported_source_blocklist_entries():
    get_db().execute(
        "DELETE FROM blocklist WHERE reason = ?;",
        (2,) # Source not supported
    )
    return


@DatabaseMigrationHandler.register_handler(21)
def _migrate_add_pixel_drain_to_preference():
    # This migration used to add WeTransfer to the service preference list.
    # This setting doesn't exist anymore, as it was moved to the indexer
    # setting.
    return


@DatabaseMigrationHandler.register_handler(22)
def _migrate_add_links_in_download_queue():
    get_db().executescript("""
        DROP TABLE download_queue;
        CREATE TABLE download_queue(
            id INTEGER PRIMARY KEY,
            client_type VARCHAR(255) NOT NULL,
            torrent_client_id INTEGER,

            download_link TEXT NOT NULL,
            filename_body TEXT NOT NULL,
            source VARCHAR(25) NOT NULL,

            volume_id INTEGER NOT NULL,
            issue_id INTEGER,
            web_link TEXT,
            web_title TEXT,
            web_sub_title TEXT,

            FOREIGN KEY (torrent_client_id) REFERENCES torrent_clients(id),
            FOREIGN KEY (volume_id) REFERENCES volumes(id),
            FOREIGN KEY (issue_id) REFERENCES issues(id)
        );
    """)
    return


@DatabaseMigrationHandler.register_handler(23)
def _migrate_service_preference_to_enum_values():
    # This migration used to add WeTransfer to the service preference list.
    # This setting doesn't exist anymore, as it was moved to the indexer
    # setting.
    return


@DatabaseMigrationHandler.register_handler(24)
def _migrate_add_links_in_blocklist():
    get_db().executescript("""
        BEGIN TRANSACTION;
        PRAGMA defer_foreign_keys = ON;

        CREATE TEMPORARY TABLE temp_blocklist_25 AS
            SELECT * FROM blocklist;
        DROP TABLE blocklist;

        CREATE TABLE blocklist(
            id INTEGER PRIMARY KEY,
            volume_id INTEGER,
            issue_id INTEGER,

            web_link TEXT,
            web_title TEXT,
            web_sub_title TEXT,

            download_link TEXT UNIQUE,
            source VARCHAR(30),

            reason INTEGER NOT NULL CHECK (reason > 0),
            added_at INTEGER NOT NULL CHECK (added_at > 0),

            FOREIGN KEY (volume_id) REFERENCES volumes(id),
            FOREIGN KEY (issue_id) REFERENCES issues(id)
        );

        INSERT INTO blocklist
            SELECT
                id,
                NULL AS volume_id,
                NULL AS issue_id,
                NULL AS web_link,
                NULL AS web_title,
                NULL AS web_sub_title,
                link AS download_link,
                NULL AS source,
                reason,
                added_at
            FROM temp_blocklist_25
            WHERE link LIKE 'https://getcomics.org/dlds%';

        INSERT INTO blocklist
            SELECT
                id,
                NULL AS volume_id,
                NULL AS issue_id,
                link AS web_link,
                NULL AS web_title,
                NULL AS web_sub_title,
                NULL AS download_link,
                NULL AS source,
                reason,
                added_at
            FROM temp_blocklist_25
            WHERE NOT link LIKE 'https://getcomics.org/dlds%';

        COMMIT;
    """)
    return


@DatabaseMigrationHandler.register_handler(25)
def _migrate_add_links_in_history():
    get_db().executescript("""
        BEGIN TRANSACTION;
        PRAGMA defer_foreign_keys = ON;

        CREATE TEMPORARY TABLE temp_download_history_26 AS
            SELECT * FROM download_history;
        DROP TABLE download_history;

        CREATE TABLE download_history(
            web_link TEXT,
            web_title TEXT,
            web_sub_title TEXT,
            file_title TEXT,

            volume_id INTEGER,
            issue_id INTEGER,

            source VARCHAR(25),
            downloaded_at INTEGER NOT NULL CHECK (downloaded_at > 0),

            FOREIGN KEY (volume_id) REFERENCES volumes(id),
            FOREIGN KEY (issue_id) REFERENCES issues(id)
        );

        INSERT INTO download_history
            SELECT
                original_link AS web_link,
                title AS web_title,
                NULL AS web_sub_title,
                NULL AS file_title,
                NULL AS volume_id,
                NULL AS issue_id,
                NULL AS source,
                downloaded_at
            FROM temp_download_history_26;

        COMMIT;
    """)
    return


@DatabaseMigrationHandler.register_handler(26)
def _migrate_add_foreign_keys_to_history_and_blocklist():
    get_db().executescript("""
        BEGIN TRANSACTION;
        PRAGMA defer_foreign_keys = ON;

        CREATE TEMPORARY TABLE temp_download_history_27 AS
            SELECT * FROM download_history;
        DROP TABLE download_history;

        CREATE TABLE download_history(
            web_link TEXT,
            web_title TEXT,
            web_sub_title TEXT,
            file_title TEXT,

            volume_id INTEGER,
            issue_id INTEGER,

            source VARCHAR(25),
            downloaded_at INTEGER NOT NULL CHECK (downloaded_at > 0),

            FOREIGN KEY (volume_id) REFERENCES volumes(id)
                ON DELETE SET NULL,
            FOREIGN KEY (issue_id) REFERENCES issues(id)
                ON DELETE SET NULL
        );

        INSERT INTO download_history
            SELECT *
            FROM temp_download_history_27;

        CREATE TEMPORARY TABLE temp_blocklist_27 AS
            SELECT * FROM blocklist;
        DROP TABLE blocklist;

        CREATE TABLE blocklist(
            id INTEGER PRIMARY KEY,
            volume_id INTEGER,
            issue_id INTEGER,

            web_link TEXT,
            web_title TEXT,
            web_sub_title TEXT,

            download_link TEXT UNIQUE,
            source VARCHAR(30),

            reason INTEGER NOT NULL CHECK (reason > 0),
            added_at INTEGER NOT NULL CHECK (added_at > 0),

            FOREIGN KEY (volume_id) REFERENCES volumes(id)
                ON DELETE SET NULL,
            FOREIGN KEY (issue_id) REFERENCES issues(id)
                ON DELETE SET NULL
        );

        INSERT INTO blocklist
            SELECT *
            FROM temp_blocklist_27;

        COMMIT;
    """)
    return


@DatabaseMigrationHandler.register_handler(27)
def _migrate_add_site_url_to_volumes():
    get_db().execute("""
        ALTER TABLE volumes ADD
            site_url TEXT NOT NULL DEFAULT "";
    """)
    return


@DatabaseMigrationHandler.register_handler(28)
def _migrate_add_alt_title_to_volumes():
    get_db().execute("""
        ALTER TABLE volumes ADD
            alt_title VARCHAR(255);
    """)
    return


@DatabaseMigrationHandler.register_handler(29)
def _migrate_none_to_string_flare_solverr():
    cursor = get_db()
    value = cursor.execute("""
        SELECT value
        FROM config
        WHERE key = 'flaresolverr_base_url'
        LIMIT 1;
        """
    ).fetchone()['value']

    if not value:
        cursor.execute("""
            UPDATE config
            SET value = ''
            WHERE key = 'flaresolverr_base_url';
        """)
    return


@DatabaseMigrationHandler.register_handler(30)
def _migrate_remove_unused_settings():

    # This migration would remove unused settings, but one of those was
    # used in migration V31 -> V32, so removing the unused settings was
    # moved to that migration, after using the setting. But because people
    # already ran this migration, their database version already updated to
    # 31, so this migration couldn't be removed.

    return


@DatabaseMigrationHandler.register_handler(31)
def _migrate_vai_naming():
    from backend.internals.settings import SettingsValues

    cursor = get_db()

    volume_as_empty = (cursor.execute(
        "SELECT value FROM config WHERE key = 'volume_as_empty' LIMIT 1;"
    ).fetchone() or (None,))[0]
    if volume_as_empty:
        cursor.execute(
            "UPDATE config SET value = ? WHERE key = 'file_naming_vai';",
            ('{series_name} ({year}) Volume {volume_number} Issue {issue_number}',)
        )

    cursor.execute("SELECT key FROM config")
    delete_keys = [
        key
        for key in cursor
        if key[0] not in SettingsValues.__dataclass_fields__
    ]
    cursor.executemany(
        "DELETE FROM config WHERE key = ?;",
        delete_keys
    )

    return


@DatabaseMigrationHandler.register_handler(32)
def _migrate_credentials():
    get_db().executescript("""
        BEGIN TRANSACTION;
        PRAGMA defer_foreign_keys = ON;

        CREATE TEMPORARY TABLE temp_credentials_33 AS
            SELECT * FROM credentials;
        DROP TABLE credentials;

        DROP TABLE credentials_sources;

        CREATE TABLE IF NOT EXISTS credentials(
            id INTEGER PRIMARY KEY,
            source VARCHAR(30) NOT NULL UNIQUE,
            username TEXT,
            email TEXT,
            password TEXT,
            api_key TEXT
        );

        INSERT INTO credentials(id, source, email, password)
            SELECT id, 'mega' AS source, email, password
            FROM temp_credentials_33;

        COMMIT;
    """)

    return


@DatabaseMigrationHandler.register_handler(33)
def _migrate_external_download_clients():
    get_db().executescript(
        """
        BEGIN TRANSACTION;
        PRAGMA defer_foreign_keys = ON;

        CREATE TEMPORARY TABLE temp_download_queue_34 AS
            SELECT * FROM download_queue;
        DROP TABLE download_queue;

        CREATE TABLE IF NOT EXISTS download_queue(
            id INTEGER PRIMARY KEY,
            client_type VARCHAR(255) NOT NULL,
            external_client_id INTEGER,

            download_link TEXT NOT NULL,
            filename_body TEXT NOT NULL,
            source VARCHAR(25) NOT NULL,

            volume_id INTEGER NOT NULL,
            issue_id INTEGER,
            web_link TEXT,
            web_title TEXT,
            web_sub_title TEXT,

            FOREIGN KEY (external_client_id) REFERENCES external_download_clients(id),
            FOREIGN KEY (volume_id) REFERENCES volumes(id),
            FOREIGN KEY (issue_id) REFERENCES issues(id)
        );

        INSERT INTO download_queue(
            id, client_type, external_client_id,
            download_link, filename_body, source,
            volume_id, issue_id,
            web_link, web_title, web_sub_title
        )
            SELECT
                id, client_type, torrent_client_id AS external_client_id,
                download_link, filename_body, source,
                volume_id, issue_id,
                web_link, web_title, web_sub_title
            FROM temp_download_queue_34;

        CREATE TEMPORARY TABLE temp_torrent_clients_34 AS
            SELECT * FROM torrent_clients;
        DROP TABLE torrent_clients;

        INSERT INTO external_download_clients(
            id, download_type, client_type, title, base_url,
            username, password, api_token
        )
            SELECT
                id, 2 AS download_type, type AS client_type, title, base_url,
                username, password, api_token
            FROM temp_torrent_clients_34;

        COMMIT;
    """)

    return


@DatabaseMigrationHandler.register_handler(34)
def _migrate_type_hosting_settings():
    from backend.internals.settings import Settings

    cursor = get_db()
    port = cursor.execute(
        "SELECT value FROM config WHERE key = 'port' LIMIT 1;"
    ).fetchone()[0]

    cursor.execute(
        "UPDATE config SET value=? WHERE key = 'port';",
        (int(port),)
    )

    settings = Settings()
    settings.clear_cache()
    settings.backup_hosting_settings()
    return


@DatabaseMigrationHandler.register_handler(35)
def _migrate_download_queue_to_refactor():
    get_db().executescript("""
        DROP TABLE download_queue;
        CREATE TABLE IF NOT EXISTS download_queue(
            id INTEGER PRIMARY KEY,
            volume_id INTEGER NOT NULL,
            client_type VARCHAR(255) NOT NULL,
            external_client_id INTEGER,

            download_link TEXT NOT NULL,
            covered_issues VARCHAR(255),
            force_original_name BOOL,

            source_type VARCHAR(25) NOT NULL,
            source_name VARCHAR(255) NOT NULL,

            web_link TEXT,
            web_title TEXT,
            web_sub_title TEXT,

            FOREIGN KEY (external_client_id) REFERENCES external_download_clients(id),
            FOREIGN KEY (volume_id) REFERENCES volumes(id)
        );
    """)
    return


@DatabaseMigrationHandler.register_handler(36)
def _migrate_multiple_credentials():
    get_db().executescript("""
        BEGIN TRANSACTION;
        PRAGMA defer_foreign_keys = ON;

        CREATE TEMPORARY TABLE temp_credentials_37 AS
            SELECT * FROM credentials;
        DROP TABLE credentials;

        CREATE TABLE IF NOT EXISTS credentials(
            id INTEGER PRIMARY KEY,
            source VARCHAR(30) NOT NULL,
            username TEXT,
            email TEXT,
            password TEXT,
            api_key TEXT
        );

        INSERT INTO credentials
            SELECT * FROM temp_credentials_37;

        COMMIT;
    """)
    return


@DatabaseMigrationHandler.register_handler(37)
def _migrate_add_monitor_new_issues_to_volumes():
    get_db().execute("""
        ALTER TABLE volumes ADD COLUMN
            monitor_new_issues BOOL NOT NULL DEFAULT 1;
    """)
    return


@DatabaseMigrationHandler.register_handler(38)
def _migrate_torrent_timeout_to_download_timeout():
    cursor = get_db()

    old_value = cursor.execute(
        "SELECT value FROM config WHERE key = 'failing_torrent_timeout' LIMIT 1;"
    ).fetchone()[0]

    cursor.execute(
        "UPDATE config SET value = ? WHERE key = 'failing_download_timeout';",
        (old_value,))

    cursor.execute(
        "DELETE FROM config WHERE key = 'failing_torrent_timeout';"
    )

    return


@DatabaseMigrationHandler.register_handler(39)
def _migrate_delete_completed_torrents_to_downloads():
    cursor = get_db()

    old_value = cursor.execute(
        "SELECT value FROM config WHERE key = 'delete_completed_torrents' LIMIT 1;"
    ).fetchone()[0]

    cursor.execute(
        "UPDATE config SET value = ? WHERE key = 'delete_completed_downloads';",
        (old_value,))

    cursor.execute(
        "DELETE FROM config WHERE key = 'delete_completed_torrents';"
    )

    return


@DatabaseMigrationHandler.register_handler(40)
def _migrate_hash_password():
    from backend.internals.settings import Settings

    s = Settings()
    settings = s.get_settings()

    if settings.auth_password:
        s.update({"auth_password": settings.auth_password})

    return


@DatabaseMigrationHandler.register_handler(41)
def _migrate_add_success_to_download_history():
    get_db().execute("""
        ALTER TABLE download_history ADD COLUMN
            success BOOL;
    """)

    return


@DatabaseMigrationHandler.register_handler(42)
def _migrate_seperate_covers_table():
    cursor = get_db()

    cursor.executescript("""
        PRAGMA foreign_keys = OFF;
        BEGIN TRANSACTION;

        INSERT OR IGNORE INTO volumes_covers(volume_id, cover)
            SELECT id, cover
            FROM volumes;

        CREATE TEMPORARY TABLE temp_volumes_43 AS SELECT
            id,
            comicvine_id,
            title,
            alt_title,
            year,
            publisher,
            volume_number,
            description,
            site_url,
            monitored,
            monitor_new_issues,
            root_folder,
            folder,
            custom_folder,
            last_cv_fetch,
            special_version,
            special_version_locked
        FROM volumes;
        DROP TABLE volumes;

        CREATE TABLE volumes(
            id INTEGER PRIMARY KEY,
            comicvine_id INTEGER NOT NULL,
            title VARCHAR(255) NOT NULL,
            alt_title VARCHAR(255),
            year INTEGER(5),
            publisher VARCHAR(255),
            volume_number INTEGER(8) DEFAULT 1,
            description TEXT,
            site_url TEXT NOT NULL DEFAULT "",
            monitored BOOL NOT NULL DEFAULT 0,
            monitor_new_issues BOOL NOT NULL DEFAULT 1,
            root_folder INTEGER NOT NULL,
            folder TEXT,
            custom_folder BOOL NOT NULL DEFAULT 0,
            last_cv_fetch INTEGER(8) DEFAULT 0,
            special_version VARCHAR(255),
            special_version_locked BOOL NOT NULL DEFAULT 0,

            FOREIGN KEY (root_folder) REFERENCES root_folders(id)
        );

        INSERT INTO volumes
            SELECT *
            FROM temp_volumes_43;

        COMMIT;
        PRAGMA foreign_keys = ON;
    """)

    return


@DatabaseMigrationHandler.register_handler(43)
def _migrate_remove_unsupported_source_blocklist_entries():
    get_db().execute(
        "DELETE FROM blocklist WHERE reason = ?;",
        (2,) # Source not supported
    )
    return


@DatabaseMigrationHandler.register_handler(44)
def _migrate_add_forced_file_match_column():
    get_db().executescript("""
        ALTER TABLE issues_files ADD COLUMN
            forced BOOL NOT NULL DEFAULT 0;

        ALTER TABLE volume_files ADD COLUMN
            forced BOOL NOT NULL DEFAULT 0;
    """)

    return


@DatabaseMigrationHandler.register_handler(45)
def _migrate_add_enabled_option_to_ec():
    get_db().execute("""
        ALTER TABLE external_download_clients
            ADD COLUMN enabled BOOL NOT NULL DEFAULT 1;
    """)

    return


@DatabaseMigrationHandler.register_handler(46)
def _migrate_blocklist_source_to_download_service():
    get_db().execute("""
        ALTER TABLE blocklist
            RENAME COLUMN source TO download_service;
    """)

    return


@DatabaseMigrationHandler.register_handler(47)
def _migrate_add_gc_indexer() -> None:
    cursor = get_db()

    service_preference = cursor.execute(
        "SELECT value FROM config WHERE key = 'service_preference';"
    ).exists()

    get_db().execute("""
        INSERT INTO indexer_clients(
            enabled,
            download_type, client_type,
            title, url,
            gc_service_preference,
            gc_avoid_large_downloads
        ) VALUES (
            :enabled,
            :download_type, :client_type,
            :title, :url,
            :gc_service_preference,
            :gc_avoid_large_downloads
        );
        """,
        {
            "enabled": True,
            "download_type": DownloadType.DDL,
            "client_type": "GetComics",
            "title": "GetComics",
            "url": "https://getcomics.org",
            "gc_service_preference": service_preference,
            "gc_avoid_large_downloads": False
        }
    )

    cursor.execute(
        "DELETE FROM config WHERE key = 'service_preference';"
    )

    return


@DatabaseMigrationHandler.register_handler(48)
def _migrate_fix_avoid_large_downloads() -> None:
    get_db().execute(
        "UPDATE indexer_clients SET gc_avoid_large_downloads=0;"
    )

    return


@DatabaseMigrationHandler.register_handler(49)
def _migrate_task_intervals_to_schedule() -> None:
    from backend.base.helpers import get_schedules_next_run
    from backend.features.tasks import TASK_INTERVALS

    cursor = get_db()

    cursor.execute("DROP TABLE task_intervals;")
    cursor.execute("""
        CREATE TABLE task_intervals(
            task_name PRIMARY KEY,
            schedule TEXT NOT NULL,
            next_run INTEGER NOT NULL
        );
    """)

    cursor.executemany(
        """
        INSERT INTO task_intervals(task_name, schedule, next_run)
        VALUES (?, ?, ?);
        """,
        (
            (task_name, schedule, get_schedules_next_run(schedule))
            for task_name, schedule in TASK_INTERVALS.items()
        )
    )

    return


@DatabaseMigrationHandler.register_handler(50)
def _migrate_remove_search_all_task() -> None:
    get_db().execute(
        "DELETE FROM task_intervals WHERE task_name='search_all';"
    )

    return


@DatabaseMigrationHandler.register_handler(51)
def _migrate_provider_identity_storage() -> None:
    """Lossless schema-51 shadows, including version, in one atomic unit.

    The framework commits before this handler. Updating the version here too
    closes the crash window before its normal version assignment. On failure,
    rollback explicitly: app-context teardown otherwise commits pending work.
    """
    from backend.internals.identity_schema import (IDENTITY_TABLES,
                                                   IDENTITY_TRIGGERS,
                                                   METADATA_PROVIDER_COLUMN)

    cursor = get_db()
    cursor.execute('SAVEPOINT provider_identity_52')
    try:
        cursor.execute('ALTER TABLE volumes ADD COLUMN ' + METADATA_PROVIDER_COLUMN)
        for statement in IDENTITY_TABLES:
            cursor.execute(statement)
        cursor.execute("""INSERT INTO volume_external_ids
            (volume_id,provider,provider_id,provenance,last_fetch)
            SELECT id,'comicvine',CAST(comicvine_id AS TEXT),'migration',last_cv_fetch
            FROM volumes""")
        cursor.execute("""INSERT INTO issue_external_ids
            (issue_id,provider,provider_id,provenance)
            SELECT id,'comicvine',CAST(comicvine_id AS TEXT),'migration'
            FROM issues""")
        for statement in IDENTITY_TRIGGERS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Provider identity migration: foreign key violation')
        cursor.execute("UPDATE config SET value=52 WHERE key='database_version'")
        cursor.execute('RELEASE provider_identity_52')
    except BaseException:
        cursor.execute('ROLLBACK TO provider_identity_52')
        cursor.execute('RELEASE provider_identity_52')
        raise


@DatabaseMigrationHandler.register_handler(52)
def _migrate_nullable_provider_identity() -> None:
    """Rebuild four tables without touching canonical keys or child rows."""
    from backend.internals.provider_schema import TRIGGERS, relax_definition

    cursor = get_db()
    connection = cursor.connection
    if connection.in_transaction:
        raise RuntimeError('Schema 53 requires the framework commit boundary')
    fk_enabled = cursor.execute('PRAGMA foreign_keys').fetchone()[0]
    cursor.execute('PRAGMA foreign_keys=OFF')
    cursor.execute('SAVEPOINT provider_identity_53')
    try:
        tables = ('volumes', 'issues', 'volume_external_ids', 'issue_external_ids')
        indexes = [row[0] for row in cursor.execute('''SELECT sql FROM sqlite_master
            WHERE type='index' AND sql IS NOT NULL AND tbl_name IN (?,?,?,?)''', tables)]
        extra_triggers = [row[0] for row in cursor.execute('''SELECT sql FROM sqlite_master
            WHERE type='trigger' AND tbl_name IN (?,?,?,?) AND name NOT IN
            ('volumes_identity_insert','volumes_identity_update',
             'issues_identity_insert','issues_identity_update')''', tables)]
        for table in tables:
            sql = cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            # CREATE replacement first; never rename the old parent (FK rewrite).
            definition = relax_definition(sql[sql.index('('):])
            cursor.execute(f'CREATE TABLE _provider53_{table}' + definition)
            cursor.execute(f'INSERT INTO _provider53_{table} SELECT * FROM {table}')
        for table in tables:
            cursor.execute(f'DROP TABLE {table}')
        for table in tables:
            cursor.execute(f'ALTER TABLE _provider53_{table} RENAME TO {table}')
        for sql in indexes:
            cursor.execute(sql)
        for sql in (*TRIGGERS, *extra_triggers):
            cursor.execute(sql)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Schema 53 foreign key violation')
        cursor.execute("UPDATE config SET value=53 WHERE key='database_version'")
        cursor.execute('RELEASE provider_identity_53')
    except BaseException:
        cursor.execute('ROLLBACK TO provider_identity_53')
        cursor.execute('RELEASE provider_identity_53')
        raise
    finally:
        cursor.execute(f'PRAGMA foreign_keys={fk_enabled}')


@DatabaseMigrationHandler.register_handler(61)
def _migrate_content_coverage() -> None:
    """Install empty claims/coverage; existing direct identity is unchanged."""
    from backend.internals.content_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 62:
        return
    if version is None or int(version[0]) != 61:
        raise RuntimeError('Content migration requires schema 61')
    cursor.execute('SAVEPOINT content_coverage_62')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Content coverage foreign key violation')
        cursor.execute("UPDATE config SET value=62 WHERE key='database_version'")
        cursor.execute('RELEASE content_coverage_62')
    except BaseException:
        cursor.execute('ROLLBACK TO content_coverage_62')
        cursor.execute('RELEASE content_coverage_62')
        raise


@DatabaseMigrationHandler.register_handler(62)
def _migrate_classification_provenance() -> None:
    """Empty provenance only: never evaluate or reconstruct historical origin."""
    from backend.internals.classification_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 63:
        return
    if version is None or int(version[0]) != 62:
        raise RuntimeError('Classification migration requires schema 62')
    cursor.execute('SAVEPOINT classification_63')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Classification foreign key violation')
        cursor.execute("UPDATE config SET value=63 WHERE key='database_version'")
        cursor.execute('RELEASE classification_63')
    except BaseException:
        cursor.execute('ROLLBACK TO classification_63')
        cursor.execute('RELEASE classification_63')
        raise


@DatabaseMigrationHandler.register_handler(63)
def _migrate_provider_switching() -> None:
    """Initialize generation zero and empty receipts; no authority or metadata rewrite."""
    from backend.internals.provider_switch_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 64:
        return
    if version is None or int(version[0]) != 63:
        raise RuntimeError('Provider switch migration requires schema 63')
    cursor.execute('SAVEPOINT provider_switch_64')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Provider switch foreign key violation')
        cursor.execute("UPDATE config SET value=64 WHERE key='database_version'")
        cursor.execute('RELEASE provider_switch_64')
    except BaseException:
        cursor.execute('ROLLBACK TO provider_switch_64')
        cursor.execute('RELEASE provider_switch_64')
        raise


@DatabaseMigrationHandler.register_handler(64)
def _migrate_metadata_repair() -> None:
    """Empty successful-repair history only; no metadata or filesystem changes."""
    from backend.internals.metadata_repair_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 65:
        return
    if version is None or int(version[0]) != 64:
        raise RuntimeError('Metadata repair migration requires schema 64')
    cursor.execute('SAVEPOINT metadata_repair_65')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Metadata repair foreign key violation')
        cursor.execute("UPDATE config SET value=65 WHERE key='database_version'")
        cursor.execute('RELEASE metadata_repair_65')
    except BaseException:
        cursor.execute('ROLLBACK TO metadata_repair_65')
        cursor.execute('RELEASE metadata_repair_65')
        raise


@DatabaseMigrationHandler.register_handler(65)
def _migrate_file_quarantine() -> None:
    """Retain inactive file identity; no files/links/history are changed here."""
    from backend.internals.quarantine_schema import STATEMENTS

    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 66:
        return
    if version is None or int(version[0]) != 65:
        raise RuntimeError('File quarantine migration requires schema 65')
    cursor.execute('SAVEPOINT file_quarantine_66')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('File quarantine foreign key violation')
        cursor.execute("UPDATE config SET value=66 WHERE key='database_version'")
        cursor.execute('RELEASE file_quarantine_66')
    except BaseException:
        cursor.execute('ROLLBACK TO file_quarantine_66')
        cursor.execute('RELEASE file_quarantine_66')
        raise


@DatabaseMigrationHandler.register_handler(66)
def _migrate_collections() -> None:
    """Local organization only; no inferred membership or provider acquisition."""
    from backend.internals.collections_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 67:
        return
    if version is None or int(version[0]) != 66:
        raise RuntimeError('Collections migration requires schema 66')
    cursor.execute('SAVEPOINT collections_67')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Collections foreign key violation')
        cursor.execute("UPDATE config SET value=67 WHERE key='database_version'")
        cursor.execute('RELEASE collections_67')
    except BaseException:
        cursor.execute('ROLLBACK TO collections_67')
        cursor.execute('RELEASE collections_67')
        raise


@DatabaseMigrationHandler.register_handler(67)
def _migrate_release_calendar() -> None:
    """Add observational release evidence without changing library or Collections."""
    from backend.internals.release_calendar_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 68:
        return
    if version is None or int(version[0]) != 67:
        raise RuntimeError('Calendar migration requires schema 67')
    cursor.execute('SAVEPOINT calendar_68')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Calendar foreign key violation')
        cursor.execute("UPDATE config SET value=68 WHERE key='database_version'")
        cursor.execute('RELEASE calendar_68')
    except BaseException:
        cursor.execute('ROLLBACK TO calendar_68')
        cursor.execute('RELEASE calendar_68')
        raise


@DatabaseMigrationHandler.register_handler(68)
def _migrate_reading_orders() -> None:
    """Local ordered references only; no library backfill or network access."""
    from backend.internals.reading_orders_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 69:
        return
    if version is None or int(version[0]) != 68:
        raise RuntimeError('Reading Orders migration requires schema 68')
    cursor.execute('SAVEPOINT reading_orders_69')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Reading Orders foreign key violation')
        cursor.execute("UPDATE config SET value=69 WHERE key='database_version'")
        cursor.execute('RELEASE reading_orders_69')
    except BaseException:
        cursor.execute('ROLLBACK TO reading_orders_69')
        cursor.execute('RELEASE reading_orders_69')
        raise


@DatabaseMigrationHandler.register_handler(70)
def _migrate_discovery() -> None:
    """Observation cache only: no polling, matching or acquisition."""
    from backend.internals.discovery_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 71:
        return
    if version is None or int(version[0]) != 70:
        raise RuntimeError('Discovery migration requires schema 70')
    cursor.execute('SAVEPOINT discovery_71')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Discovery foreign key violation')
        cursor.execute("UPDATE config SET value=71 WHERE key='database_version'")
        cursor.execute('RELEASE discovery_71')
    except BaseException:
        cursor.execute('ROLLBACK TO discovery_71')
        cursor.execute('RELEASE discovery_71')
        raise


@DatabaseMigrationHandler.register_handler(71)
def _migrate_expanded_clients() -> None:
    """No client configuration, network work or cleanup is enabled by migration."""
    from backend.internals.torrent_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 72:
        return
    if version is None or int(version[0]) != 71:
        raise RuntimeError('Expanded clients migration requires schema 71')
    cursor.execute('SAVEPOINT expanded_clients_72')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Expanded clients foreign key violation')
        cursor.execute("UPDATE config SET value=72 WHERE key='database_version'")
        cursor.execute('RELEASE expanded_clients_72')
    except BaseException:
        cursor.execute('ROLLBACK TO expanded_clients_72')
        cursor.execute('RELEASE expanded_clients_72')
        raise


@DatabaseMigrationHandler.register_handler(69)
def _migrate_quality_profiles() -> None:
    """Add opt-in policy; no scan, monitoring mutation or acquisition work."""
    from backend.internals.quality_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 70:
        return
    if version is None or int(version[0]) != 69:
        raise RuntimeError('Quality migration requires schema 69')
    cursor.execute('SAVEPOINT quality_70')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Quality foreign key violation')
        cursor.execute("UPDATE config SET value=70 WHERE key='database_version'")
        cursor.execute('RELEASE quality_70')
    except BaseException:
        cursor.execute('ROLLBACK TO quality_70')
        cursor.execute('RELEASE quality_70')
        raise


@DatabaseMigrationHandler.register_handler(60)
def _migrate_reprint_graph() -> None:
    """Empty graph schema; never reads an external catalog during migration."""
    from backend.internals.reprint_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 61:
        return
    if version is None or int(version[0]) != 60:
        raise RuntimeError('Graph migration requires schema 60')
    cursor.execute('SAVEPOINT reprint_graph_61')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Graph foreign key violation')
        cursor.execute("UPDATE config SET value=61 WHERE key='database_version'")
        cursor.execute('RELEASE reprint_graph_61')
    except BaseException:
        cursor.execute('ROLLBACK TO reprint_graph_61')
        cursor.execute('RELEASE reprint_graph_61')
        raise


@DatabaseMigrationHandler.register_handler(59)
def _migrate_bibliography() -> None:
    """Add empty bibliography storage; no remote backfill or identity changes."""
    from backend.internals.bibliography_schema import STATEMENTS
    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 60:
        return
    if version is None or int(version[0]) != 59:
        raise RuntimeError('Bibliography migration requires schema 59')
    cursor.execute('SAVEPOINT bibliography_60')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Bibliography foreign key violation')
        cursor.execute("UPDATE config SET value=60 WHERE key='database_version'")
        cursor.execute('RELEASE bibliography_60')
    except BaseException:
        cursor.execute('ROLLBACK TO bibliography_60')
        cursor.execute('RELEASE bibliography_60')
        raise


@DatabaseMigrationHandler.register_handler(53)
def _migrate_organization_journal() -> None:
    """Intent, receipts and version advance together; no library row changes."""
    from backend.internals.organization_schema import STATEMENTS

    cursor = get_db()
    cursor.execute('SAVEPOINT organization_journal_54')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Organization journal foreign key violation')
        cursor.execute("UPDATE config SET value=54 WHERE key='database_version'")
        cursor.execute('RELEASE organization_journal_54')
    except BaseException:
        cursor.execute('ROLLBACK TO organization_journal_54')
        cursor.execute('RELEASE organization_journal_54')
        raise


@DatabaseMigrationHandler.register_handler(54)
def _migrate_folder_monitor() -> None:
    """Add observation state without touching library or organizer history."""
    from backend.internals.monitor_schema import STATEMENTS

    cursor = get_db()
    cursor.execute('SAVEPOINT folder_monitor_55')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Folder monitor foreign key violation')
        cursor.execute("UPDATE config SET value=55 WHERE key='database_version'")
        cursor.execute('RELEASE folder_monitor_55')
    except BaseException:
        cursor.execute('ROLLBACK TO folder_monitor_55')
        cursor.execute('RELEASE folder_monitor_55')
        raise


@DatabaseMigrationHandler.register_handler(55)
def _migrate_acquisition_downloads() -> None:
    """Durable remote receipts; existing queue/library identities are untouched."""
    from backend.internals.download_schema import STATEMENTS

    cursor = get_db()
    cursor.execute('SAVEPOINT acquisition_downloads_56')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Acquisition download foreign key violation')
        cursor.execute("UPDATE config SET value=56 WHERE key='database_version'")
        cursor.execute('RELEASE acquisition_downloads_56')
    except BaseException:
        cursor.execute('ROLLBACK TO acquisition_downloads_56')
        cursor.execute('RELEASE acquisition_downloads_56')
        raise


@DatabaseMigrationHandler.register_handler(56)
def _migrate_acquisition_intake() -> None:
    """Completion/artifact correlations only; existing journals remain intact."""
    from backend.internals.intake_schema import STATEMENTS

    cursor = get_db()
    cursor.execute('SAVEPOINT acquisition_intake_57')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Acquisition intake foreign key violation')
        cursor.execute("UPDATE config SET value=57 WHERE key='database_version'")
        cursor.execute('RELEASE acquisition_intake_57')
    except BaseException:
        cursor.execute('ROLLBACK TO acquisition_intake_57')
        cursor.execute('RELEASE acquisition_intake_57')
        raise


@DatabaseMigrationHandler.register_handler(57)
def _migrate_wanted_automation() -> None:
    """Add local-ID search scheduling/claims without changing monitoring intent."""
    from backend.internals.wanted_schema import STATEMENTS

    cursor = get_db()
    cursor.execute('SAVEPOINT wanted_58')
    try:
        for statement in STATEMENTS:
            cursor.execute(statement)
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Wanted automation foreign key violation')
        cursor.execute("UPDATE config SET value=58 WHERE key='database_version'")
        cursor.execute('RELEASE wanted_58')
    except BaseException:
        cursor.execute('ROLLBACK TO wanted_58')
        cursor.execute('RELEASE wanted_58')
        raise


@DatabaseMigrationHandler.register_handler(58)
def _migrate_canonical_issue_facts() -> None:
    """Preserve IDs/projections; certify only persisted mapped evidence."""
    from backend.internals.issue_facts import mapped_facts, write_facts
    from backend.internals.issue_facts_schema import (STATEMENTS,
                                                      nullable_projection)

    cursor = get_db()
    version = cursor.execute("SELECT value FROM config WHERE key='database_version'").fetchone()
    if version is not None and int(version[0]) == 59:
        return
    if version is None or int(version[0]) != 58:
        raise RuntimeError('Canonical issue migration requires schema 58')
    if cursor.connection.in_transaction:
        raise RuntimeError('Schema 59 requires the framework commit boundary')
    fk_enabled = cursor.execute('PRAGMA foreign_keys').fetchone()[0]
    cursor.execute('PRAGMA foreign_keys=OFF')
    cursor.execute('SAVEPOINT issue_facts_59')
    try:
        sql = cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='issues'").fetchone()[0]
        if nullable_projection(sql) != sql:
            objects = cursor.execute("""SELECT sql FROM sqlite_master WHERE tbl_name='issues'
                AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name""").fetchall()
            cursor.execute('CREATE TABLE _issue59' + nullable_projection(sql[sql.index('('):]))
            cursor.execute('INSERT INTO _issue59 SELECT * FROM issues')
            cursor.execute('DROP TABLE issues')
            cursor.execute('ALTER TABLE _issue59 RENAME TO issues')
            for (definition,) in objects:
                cursor.execute(definition)
        for statement in STATEMENTS:
            cursor.execute(statement)
        # Keyset batches bound memory; repeat invocation never reinterprets facts.
        after = 0
        while True:
            rows = cursor.execute('''SELECT i.id,i.issue_number,i.date FROM issues i
                LEFT JOIN issue_number_facts n ON n.issue_id=i.id
                WHERE i.id>? AND n.issue_id IS NULL ORDER BY i.id LIMIT 750''', (after,)).fetchall()
            if not rows:
                break
            for iid, label, day in rows:
                write_facts(cursor, iid, mapped_facts(label, day, 'legacy_mapped'))
            after = rows[-1][0]
        if cursor.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RuntimeError('Canonical issue facts foreign key violation')
        cursor.execute("UPDATE config SET value=59 WHERE key='database_version'")
        cursor.execute('RELEASE issue_facts_59')
    except BaseException:
        cursor.execute('ROLLBACK TO issue_facts_59')
        cursor.execute('RELEASE issue_facts_59')
        raise
    finally:
        cursor.execute(f'PRAGMA foreign_keys={fk_enabled}')
