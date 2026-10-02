# -*- coding: utf-8 -*-

"""
Library, volume and issue classes and Refresh & Scan
"""

from __future__ import annotations

from asyncio import run
from datetime import datetime, timedelta
from functools import lru_cache
from io import BytesIO
from os.path import dirname, exists, isdir, relpath
from time import time
from typing import Any, Dict, List, Mapping, Set, Tuple, Union

from typing_extensions import assert_never

from backend.base.custom_exceptions import (InvalidKeyValue, IssueNotFound,
                                            KeyNotFound, TaskForVolumeRunning,
                                            VolumeAlreadyAdded,
                                            VolumeDownloadedFor,
                                            VolumeNotFound)
from backend.base.definitions import (BaseEnum, Constants, FileData,
                                      GeneralFileData, IssueData,
                                      LibraryFilter, LibrarySorting,
                                      MonitorScheme, SpecialVersion,
                                      VolumeData)
from backend.base.files import (change_basefolder, create_folder,
                                delete_empty_child_folders,
                                delete_empty_parent_folders,
                                delete_file_folder, folder_is_inside_folder,
                                rename_file)
from backend.base.helpers import (PortablePool, extract_year_from_date,
                                  first_of_subarrays, to_number_cv_id)
from backend.base.logging import LOGGER
from backend.implementations.classification import (ClassificationIssue,
                                                    evaluate_special_version)
from backend.implementations.file_matching import scan_files
from backend.implementations.file_processing import mass_process_files
from backend.implementations.matching import match_title
from backend.implementations.metadata.enrichment import fetch_volume_result
from backend.implementations.metadata.format_evidence import (
    ProviderFormatEvidence, validate_format_evidence)
from backend.implementations.metadata.identity_enrichment import \
    persist_enrichment
from backend.implementations.metadata.persistence import (
    ProviderVolumeIdentity, issue_input, volume_input, write_issue_batch)
from backend.implementations.metadata.provider import (
    MetadataBulkVolumeProvider, MetadataVolumeProvider)
from backend.implementations.metadata.publication_evidence import (
    ProviderPublicationEvidence, validate_publication_evidence)
from backend.implementations.metadata.registry import (
    PROVIDERS, get_bulk_volume_provider, get_volume_provider)
from backend.implementations.root_folders import RootFolders
from backend.internals.db import commit, get_db
from backend.internals.db_models import FilesDB, GeneralFilesDB
from backend.internals.provider_identity import (ExternalIdentity,
                                                 MetadataIdentityError,
                                                 ProviderIdentityDB)
from backend.internals.scan_mutation import deletion_mutation
from backend.internals.server import (DownloadedStatusEvent,
                                      TaskStatusEvent, WebSocket)
from backend.internals.settings import Settings

# autopep8: off
ONE_DAY = timedelta(days=1)
THIRTY_DAYS = timedelta(days=30)
# autopep8: on


# region Issue
class Issue:
    def __init__(self, issue_id: int, check_existence: bool = False) -> None:
        """Create an instance.

        Args:
            issue_id (int): The ID of the issue.
            check_existence (bool, optional): Check whether the issue exists
                based on its ID.
                Defaults to False.

        Raises:
            IssueNotFound: The issue was not found. Can only be raised when
                check_existence is `True`.
        """
        self.id = issue_id

        if not check_existence:
            return

        issue_id = get_db().execute(
            "SELECT id FROM issues WHERE id = ? LIMIT 1;",
            (self.id,)
        ).fetchone()

        if issue_id is None:
            raise IssueNotFound(issue_id)
        return

    @classmethod
    @lru_cache(maxsize=3)
    def from_volume_and_calc_number(
        cls,
        volume_id: int,
        calculated_issue_number: float
    ) -> Issue:
        """Create an instance based on the volume ID and calculated issue number
        of the issue. The existance of the volume is checked.

        Args:
            volume_id (int): The ID of the volume that the issue is in.
            calculated_issue_number (float): The calculated issue number of
                the issue.

        Raises:
            IssueNotFound: No issue found with the given arguments.

        Returns:
            Issue: The instance.
        """
        issue_id: Union[int, None] = get_db().execute("""
            SELECT id
            FROM issues
            WHERE volume_id = ?
                AND calculated_issue_number = ?
            LIMIT 1;
            """,
            (volume_id, calculated_issue_number)
        ).exists()

        if not issue_id:
            raise IssueNotFound(-1)

        return cls(issue_id, check_existence=True)

    def get_volume_id(self) -> int:
        """Identity lookup does not require a legacy numeric projection."""
        return get_db().execute('SELECT volume_id FROM issues WHERE id=?', (self.id,)).fetchone()[0]

    def get_raw_label(self) -> str:
        return get_db().execute('SELECT issue_number FROM issues WHERE id=?', (self.id,)).fetchone()[0]

    def get_data(self) -> IssueData:
        """Get data about the issue.

        Returns:
            IssueData: The data.
        """
        data = get_db().execute(
            """
            SELECT
                id, volume_id, comicvine_id,
                issue_number, calculated_issue_number,
                title, date, description,
                monitored
            FROM issues
            WHERE id = ?
            LIMIT 1;
            """,
            (self.id,)
        ).fetchonedict() or {}

        return IssueData(
            **data,
            files=self.get_files()
        )

    def get_files(self) -> List[FileData]:
        """Get all files linked to the issue.

        Returns:
            List[FileData]: List of file data.
        """
        return FilesDB.fetch(issue_id=self.id)

    def __format_value(self, key: str, value: Any, from_public: bool) -> Any:
        """Check whether the value of an attribute is allowed and convert if
        needed.

        Args:
            key (str): Key of attribute.
            value (Any): Value of attribute.
            from_public (bool): If True, only allow attributes to be changed
                that are allowed to be changed by the user.

        Raises:
            KeyNotFound: Key doesn't exist or can't be changed.
            InvalidKeyValue: Value of the key is not allowed.

        Returns:
            Any: (Converted) Attribute value.
        """
        converted_value = value

        if from_public and key not in ('monitored',):
            raise KeyNotFound(key)

        if key == 'monitored' and not isinstance(converted_value, bool):
            raise InvalidKeyValue(key, value)

        return converted_value

    def update(
        self,
        data: Mapping[str, Any],
        from_public: bool = False
    ) -> None:
        """Change attributes of the issue, in a `dict.update()` type of way.

        Args:
            data (Mapping[str, Any]): The keys and their new values.

            from_public (bool, optional): If True, only allow attributes to be
                changed that are allowed to be changed by the user.
                Defaults to False.

        Raises:
            KeyNotFound: Key doesn't exist or can't be changed.
            InvalidKeyValue: Value of the key is not allowed.
        """
        formatted_data = {}
        for key, value in data.items():
            formatted_data[key] = self.__format_value(key, value, from_public)

        cursor = get_db()
        for key, value in formatted_data.items():
            cursor.execute(
                f"UPDATE issues SET {key} = ? WHERE id = ?;",
                (value, self.id)
            )

        LOGGER.info(
            f'For issue {self.id}, changed: {formatted_data}'
        )
        return

    def delete(self) -> None:
        """Delete the issue from the database"""
        with deletion_mutation(get_db(), issue_id=self.id):
            self._delete_guarded()

    def _delete_guarded(self) -> None:
        LOGGER.debug(
            "Deleting issue %d with CV ID %s",
            self.id, self.get_data().comicvine_id
        )
        FilesDB.delete_issue_linked_files(self.id)
        get_db().execute(
            "DELETE FROM issues WHERE id = ?;",
            (self.id,)
        )
        return


# region Volume
class Volume:
    def __init__(self, volume_id: int, check_existence: bool = False) -> None:
        """Create an instance.

        Args:
            volume_id (int): The ID of the volume.
            check_existence (bool, optional): Check whether the volume exists
                based on its ID.
                Defaults to False.

        Raises:
            VolumeNotFound: The volume was not found. Can only be raised when
                check_existence is `True`.
        """
        self.id = volume_id

        if not check_existence:
            return

        volume_id = get_db().execute(
            "SELECT id FROM volumes WHERE id = ? LIMIT 1;",
            (self.id,)
        ).fetchone()

        if volume_id is None:
            raise VolumeNotFound(volume_id)
        return

    def get_data(self) -> VolumeData:
        """Get data about the volume.

        Returns:
            VolumeData: The data.
        """
        data = get_db().execute(
            """
            SELECT
                id, comicvine_id,
                title, alt_title,
                year, publisher, volume_number,
                description, site_url,
                monitored, monitor_new_issues,
                root_folder, folder, custom_folder,
                special_version, special_version_locked,
                last_cv_fetch
            FROM volumes
            WHERE id = ?
            LIMIT 1;
            """,
            (self.id,)
        ).fetchonedict() or {}

        data["special_version"] = SpecialVersion(data["special_version"])

        return VolumeData(**data)

    def get_public_data(self, *, rich_issues: bool = False) -> Dict[str, Any]:
        """Get data about the volume for the public to see (the API).

        Returns:
            Dict[str, Any]: The data.
        """
        volume_info = get_db().execute("""
            SELECT
                v.id, comicvine_id,
                title, year, publisher,
                volume_number,
                special_version, special_version_locked,
                description, site_url,
                monitored, monitor_new_issues,
                v.folder, root_folder,
                rf.folder AS root_folder_path,
                (
                    SELECT COUNT(*)
                    FROM issues
                    WHERE volume_id = v.id
                ) AS issue_count,
                (
                    SELECT COUNT(DISTINCT issue_id)
                    FROM issues i
                    INNER JOIN canonical_issue_files if
                    ON i.id = if.issue_id
                    WHERE volume_id = v.id
                ) AS issues_downloaded,
                (
                    SELECT SUM(size) FROM (
                        SELECT DISTINCT f.id, size
                        FROM issues i
                        INNER JOIN issues_files if
                        INNER JOIN active_files f
                        ON i.id = if.issue_id
                            AND if.file_id = f.id
                        WHERE volume_id = v.id
                    )
                ) AS total_size
            FROM volumes v
            INNER JOIN root_folders rf
            ON v.root_folder = rf.id
            WHERE v.id = ?
            LIMIT 1;
            """,
            (self.id,)
        ).fetchonedict() or {}

        volume_info['volume_folder'] = relpath(
            volume_info['folder'],
            volume_info['root_folder_path']
        )
        del volume_info['root_folder_path']

        if rich_issues:
            from backend.implementations.issue_transport import rich_issue_rows
            volume_info['issues'] = rich_issue_rows(self.id)
            from backend.implementations.issue_transport import snapshot_status
            volume_info['metadata_snapshot'] = snapshot_status(self.id)
        else:
            volume_info['issues'] = [i.todict() for i in self.get_issues()]
        volume_info['general_files'] = self.get_general_files()

        return volume_info

    # Alias, better in one-liners
    # vd = Volume Data
    @property
    def vd(self) -> VolumeData:
        return self.get_data()

    def get_cover(self) -> BytesIO:
        """Get the cover of the volume.

        Returns:
            BytesIO: The cover.
        """
        cover = get_db().execute(
            "SELECT cover FROM volumes_covers WHERE volume_id = ? LIMIT 1",
            (self.id,)
        ).fetchone()[0]
        return BytesIO(cover)

    def get_ending_year(self) -> Union[int, None]:
        """Get the year of the last issue that has a release date.

        Returns:
            Union[int, None]: The release year of the last issue with a release
                date set. `None` if there is no issue or no issue with a release
                date.
        """
        last_issue_date = get_db().execute("""
            SELECT MAX(date) AS last_issue_date
            FROM issues
            WHERE volume_id = ?;
            """,
            (self.id,)
        ).exists()

        return extract_year_from_date(last_issue_date)

    def get_issue(self, issue_id: int) -> Issue:
        """Get an issue from the volume based on its issue ID. It's checked that
        the issue exists and is part of the volume.

        Args:
            issue_id (int): The ID of the issue.

        Raises:
            IssueNotFound: Issue doesn't exist or isn't part of this volume.

        Returns:
            Issue: The issue instance.
        """
        issue = Issue(issue_id, check_existence=True)
        if issue.get_volume_id() != self.id:
            raise IssueNotFound(issue_id)
        return issue

    def get_issue_from_number(self, calculated_issue_number: float) -> Issue:
        """Get an issue from the volume based on its calculated issue number.
        It's checked that the issue exists and is part of the volume.

        Args:
            calculated_issue_number (float): The calculated issue number of the
                issue.

        Raises:
            IssueNotFound: Issue doesn't exist or isn't part of this volume.

        Returns:
            Issue: The issue instance.
        """
        return Issue.from_volume_and_calc_number(
            self.id,
            calculated_issue_number
        )

    def get_issues(self, _skip_files: bool = False) -> List[IssueData]:
        """Get a list of the issues that are in the volume.

        Args:
            _skip_files (bool, optional): Don't fetch the files matched to
                each issue. Saves quite a bit of time.
                Defaults to False.

        Returns:
            List[IssueData]: The list of issues.
        """
        cursor = get_db()
        issues = cursor.execute("""
            SELECT
                id, volume_id, comicvine_id,
                issue_number, calculated_issue_number,
                title, date, description,
                monitored
            FROM issues
            WHERE volume_id = ?
            ORDER BY date, calculated_issue_number
            """,
            (self.id,)
        ).fetchalldict()

        file_mapping: Dict[int, List[FileData]] = {}
        if not _skip_files:
            cursor.execute("""
                SELECT i.id AS issue_id, f.id AS file_id, filepath, size
                FROM active_files f
                INNER JOIN issues_files if
                    ON f.id = if.file_id
                INNER JOIN issues i
                    ON if.issue_id = i.id
                WHERE i.volume_id = ?
                ORDER BY filepath;
                """,
                (self.id,)
            )
            for file in cursor:
                file_mapping.setdefault(file[0], []).append({
                    "id": file["file_id"],
                    "filepath": file["filepath"],
                    "size": file["size"]
                })

        result = [
            IssueData(
                **i,
                files=file_mapping.get(i["id"], [])
            )
            for i in issues
        ]
        return result

    def get_open_issues(self) -> List[Tuple[int, float]]:
        """Get the issues that are not matched to a file and are monitored.

        Returns:
            List[Tuple[int, float]]: The ID and calculated issue number of
                the open issues.
        """
        return get_db().execute(
            """
            SELECT i.id, i.calculated_issue_number
            FROM issues i
            LEFT JOIN canonical_issue_files if
            ON i.id = if.issue_id
            WHERE
                file_id IS NULL
                AND volume_id = ?
                AND monitored = 1;
            """,
            (self.id,)
        ).fetchall()

    def get_all_files(self) -> List[FileData]:
        """Get the files and general files matched to the volume.

        Returns:
            List[FileData]: List of files.
        """
        result = FilesDB.fetch(volume_id=self.id)
        result.extend(GeneralFilesDB.fetch(self.id))
        return result

    def get_general_files(self) -> List[GeneralFileData]:
        """Get the general files linked to the volume.

        Returns:
            List[GeneralFileData]: The general files.
        """
        return GeneralFilesDB.fetch(self.id)

    def __format_value(self, key: str, value: Any, from_public: bool) -> Any:
        """Check whether the value of an attribute is allowed and convert if
        needed.

        Args:
            key (str): Key of attribute.
            value (Any): Value of attribute.
            from_public (bool): If True, only allow attributes to be changed
                that are allowed to be changed by the user.

        Raises:
            KeyNotFound: Key doesn't exist or can't be changed.
            InvalidKeyValue: Value of the key is not allowed.

        Returns:
            Any: (Converted) Attribute value.
        """
        if from_public:
            key_collection = (
                'monitored',
                'monitor_new_issues',
                'special_version',
                'special_version_locked'
            )

        else:
            key_collection = VolumeData.__annotations__.keys()

        # Confirm that key exists
        if key not in key_collection:
            raise KeyNotFound(key)

        if key == 'comicvine_id':
            if value is not None and not isinstance(value, int):
                raise InvalidKeyValue(key, value)
            return value

        key_data = VolumeData.__dataclass_fields__[key]

        if issubclass(key_data.type, BaseEnum):
            # Convert string to Enum value
            try:
                value = key_data.type(value)
            except ValueError:
                raise InvalidKeyValue(key, value)

        # Confirm data type of submitted value
        if not isinstance(value, key_data.type):
            raise InvalidKeyValue(key, value)

        return value

    def update(
        self,
        data: Mapping[str, Any],
        from_public: bool = False
    ) -> None:
        """Change attributes of the volume, in a `dict.update()` type of way.

        Args:
            data (Mapping[str, Any]): The keys and their new values.

            from_public (bool, optional): If True, only allow attributes to be
                changed that are allowed to be changed by the user.
                Defaults to False.

        Raises:
            KeyNotFound: Key doesn't exist or can't be changed.
            InvalidKeyValue: Value of the key is not allowed.
        """
        formatted_data = {
            key: self.__format_value(key, value, from_public)
            for key, value in data.items()
        }

        cursor = get_db()
        from contextlib import nullcontext

        from backend.internals.classification_provenance import (apply,
                                                                 control,
                                                                 transaction)
        with transaction(cursor) if {'special_version', 'special_version_locked'} & formatted_data.keys() else nullcontext():
            for key, value in formatted_data.items():
                if key == 'special_version':
                    apply(cursor, self.id, SpecialVersion(value))
                elif key == 'special_version_locked':
                    control(cursor, self.id, value)
                else:
                    cursor.execute(
                        f"UPDATE volumes SET {key} = ? WHERE id = ?;",
                        (value, self.id)
                    )

        LOGGER.info(
            f'For volume {self.id}, changed: {formatted_data}'
        )

        return

    def update_cover(self, cover: bytes) -> None:
        """Change the cover of the volume.

        Args:
            cover (bytes): The new cover image.
        """
        get_db().execute(
            """
            UPDATE volumes_covers
            SET cover = ?
            WHERE volume_id = ?;
            """,
            (cover, self.id)
        )
        return

    def apply_monitor_scheme(self, monitoring_scheme: MonitorScheme) -> None:
        """Apply a monitoring scheme to the issues of the volume.

        Args:
            monitoring_scheme (MonitorScheme): The monitoring scheme to apply.
        """
        cursor = get_db()

        if monitoring_scheme == MonitorScheme.NONE:
            cursor.execute("""
                UPDATE issues
                SET monitored = 0
                WHERE volume_id = ?;
                """,
                (self.id,)
            )

        elif monitoring_scheme == MonitorScheme.MISSING:
            cursor.execute("""
                WITH missing_issues AS (
                    SELECT id
                    FROM issues i
                    LEFT JOIN canonical_issue_files if
                    ON i.id = if.issue_id
                    WHERE volume_id = ?
                        AND if.issue_id IS NULL
                )
                UPDATE issues
                SET monitored = 0
                WHERE
                    volume_id = ?
                    AND id NOT IN missing_issues;
                """,
                (self.id, self.id)
            )

        elif monitoring_scheme == MonitorScheme.ALL:
            cursor.execute("""
                UPDATE issues
                SET monitored = 1
                WHERE volume_id = ?;
                """,
                (self.id,)
            )

        else:
            assert_never(monitoring_scheme)

        return

    def __volume_folder_used_by_other_volume(
        self,
        volume_folder: str
    ) -> bool:
        """Check whether the given volume folder is used by another volume. I.e.
        whether two volumes use the same volume folder.

        Args:
            volume_folder (str): The volume folder to check for.

        Returns:
            bool: Whether it's also used by another volume.
        """
        return get_db().execute(
            "SELECT 1 FROM volumes WHERE folder = ? AND id != ? LIMIT 1;",
            (volume_folder, self.id)
        ).exists() is not None

    def change_root_folder(self, new_root_folder_id: int) -> None:
        """Change the root folder of the volume. Updates the path in the
        database, creates the new folder (if needed) and moves the files (if any).

        Args:
            new_root_folder_id (int): The root folder ID of the new root folder.
        """
        volume_data = self.get_data()
        if volume_data.root_folder == new_root_folder_id:
            return

        root_folders = RootFolders()
        current_root_folder = root_folders[volume_data.root_folder]
        new_root_folder = root_folders[new_root_folder_id]

        LOGGER.info(
            "Changing root folder of volume %d from %s to %s",
            self.id, current_root_folder, new_root_folder
        )

        # Move files
        file_changes = change_basefolder(
            (f["filepath"] for f in self.get_all_files()),
            current_root_folder,
            new_root_folder
        )
        for old_name, new_name in file_changes.items():
            rename_file(
                old_name,
                new_name
            )
        if isdir(volume_data.folder):
            delete_empty_child_folders(volume_data.folder)

        # Update filepaths in database
        FilesDB.update_filepaths(file_changes)

        # Update volume data in database
        new_folder = change_basefolder(
            (volume_data.folder,),
            current_root_folder,
            new_root_folder
        )[volume_data.folder]
        self.update({
            'root_folder': new_root_folder_id,
            'folder': new_folder
        })

        if not self.__volume_folder_used_by_other_volume(volume_data.folder):
            # Current volume folder is not also used by another volume,
            # so we can delete it if empty.
            delete_empty_parent_folders(
                volume_data.folder,
                current_root_folder
            )

        if Settings().sv.create_empty_volume_folders:
            create_folder(new_folder)

        mass_process_files(self.id)

        return

    def change_volume_folder(
        self,
        new_volume_folder: Union[str, None]
    ) -> None:
        """Change the volume folder of the volume. Updates the path in the
        database, creates the new folder (if needed) and moves the files (if any).

        Args:
            new_volume_folder (Union[str, None]): The new folder, or `None` if
                the default folder should be generated and used.
        """
        from backend.implementations.naming import generate_volume_folder_path

        volume_data = self.get_data()
        root_folder = RootFolders()[volume_data.root_folder]
        current_volume_folder = volume_data.folder
        new_volume_folder = generate_volume_folder_path(
            root_folder, volume_data, new_volume_folder
        )

        if current_volume_folder == new_volume_folder:
            return

        LOGGER.info(
            "Changing volume folder of volume %d from %s to %s",
            self.id, current_volume_folder, new_volume_folder
        )

        # Move files
        file_changes = change_basefolder(
            (f["filepath"] for f in self.get_all_files()),
            current_volume_folder,
            new_volume_folder
        )
        for old_name, new_name in file_changes.items():
            rename_file(
                old_name,
                new_name
            )
        if isdir(current_volume_folder):
            delete_empty_child_folders(current_volume_folder)

        # Update filepaths in database
        FilesDB.update_filepaths(file_changes)

        # Update volume data in database
        self.update({
            'custom_folder': new_volume_folder is not None,
            'folder': new_volume_folder
        })

        if Settings().sv.create_empty_volume_folders:
            create_folder(new_volume_folder)

        # Delete old folder if possible
        if isdir(new_volume_folder) and folder_is_inside_folder(
            new_volume_folder, current_volume_folder
        ):
            # New folder is parent of current folder,
            # so delete up to new folder.
            delete_empty_parent_folders(
                current_volume_folder,
                new_volume_folder
            )

        elif not self.__volume_folder_used_by_other_volume(
            current_volume_folder
        ):
            # Current volume folder is not also used by another volume,
            # so we can delete it if empty.
            delete_empty_parent_folders(
                current_volume_folder,
                root_folder
            )

        mass_process_files(self.id)

        return

    def delete(self, delete_folder: bool = False) -> None:
        """Delete the volume from the library.

        Args:
            delete_folder (bool, optional): Also delete the volume folder and
                its contents.
                Defaults to False.

        Raises:
            TaskForVolumeRunning: There is a task queued for the volume.
            VolumeDownloadedFor: There is a download queued for the volume.
        """
        with deletion_mutation(get_db(), volume_id=self.id, cleanup_root=delete_folder):
            self._delete_guarded(delete_folder)

    def _delete_guarded(self, delete_folder: bool) -> None:
        from backend.features.download_queue import DownloadHandler
        from backend.features.tasks import TaskHandler

        LOGGER.info(
            "Deleting volume %d with delete_folder=%s",
            self.id, delete_folder
        )

        # Check if there is no task running for the volume
        if TaskHandler.task_for_volume_running(self.id):
            raise TaskForVolumeRunning(self.id)

        # Check if nothing is downloading for the volume
        if DownloadHandler().download_for_volume_queued(self.id):
            raise VolumeDownloadedFor(self.id)

        volume_data = self.get_data()
        if delete_folder and exists(volume_data.folder):
            for f in self.get_all_files():
                delete_file_folder(f["filepath"])

            delete_empty_child_folders(volume_data.folder)
            delete_empty_parent_folders(
                volume_data.folder,
                RootFolders()[volume_data.root_folder]
            )

        # Delete file entries
        # ON DELETE CASCADE will take care of issues_files
        FilesDB.delete_linked_files(self.id)

        # Delete metadata entries
        # ON DELETE CASCADE will take care of issues
        get_db().execute("DELETE FROM volumes WHERE id = ?", (self.id,))

        return

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__}; ID {self.id}>'


# region Library
class Library:
    @classmethod
    def get_public_volumes(
        cls,
        sort: LibrarySorting = LibrarySorting.TITLE,
        filter: Union[LibraryFilter, int, None] = None,
        *, limit: Union[int, None] = None, offset: int = 0, query: str = ''
    ) -> List[Dict[str, Any]]:
        """Get all the volumes in the library.

        Args:
            sort (LibrarySorting, optional): How to sort the list.
                Defaults to LibrarySorting.TITLE.

            filter (Union[LibraryFilter, None], optional): Apply a filter to
                the list if not `None`.
                Defaults to None.

        Returns:
            List[Dict[str, Any]]: The list of volumes in the library.
        """
        if limit is not None and (not 1 <= limit <= 100 or not 0 <= offset <= 1000000):
            raise ValueError('Invalid library page bounds')
        if query.startswith(('4050-', 'cv:')):
            try:
                filter = to_number_cv_id((query,))[0]
            except ValueError:
                return []
            query = ''
        if isinstance(filter, LibraryFilter):
            sql_filter = filter.value
        elif isinstance(filter, int):
            sql_filter = f"WHERE comicvine_id = {filter}"
        else:
            sql_filter = ''

        cursor = get_db()
        parameters: list = []
        if query:
            cursor.connection.create_function(
                'pullarr_title_contains', 2,
                lambda title, term: match_title(title or '', term, allow_contains=True),
                deterministic=True
            )
            sql_filter += (' AND ' if sql_filter else 'WHERE ') + 'pullarr_title_contains(title, ?)'
            parameters.append(query)
        paging = ''
        if limit is not None:
            paging = 'LIMIT ? OFFSET ?'
            parameters.extend((limit, offset))
        volumes = cursor.execute(f"""
            WITH
                vol_issues AS (
                    SELECT id, monitored, date
                    FROM issues
                    WHERE volume_id = volumes.id
                ),
                issues_to_files AS (
                    SELECT issue_id, monitored, f.id, size
                    FROM issues i
                    INNER JOIN issues_files if
                    INNER JOIN active_files f
                    ON i.id = if.issue_id
                        AND if.file_id = f.id
                    WHERE volume_id = volumes.id
                )
            SELECT
                id, comicvine_id,
                title, year, publisher,
                volume_number, description,
                monitored, monitor_new_issues,
                folder,
                (
                    SELECT COUNT(id) FROM vol_issues
                ) AS issue_count,
                (
                    SELECT COUNT(id) FROM vol_issues WHERE monitored = 1
                ) AS issue_count_monitored,
                (
                    SELECT COUNT(DISTINCT o.issue_id) FROM canonical_issue_files o
                    JOIN issues i ON i.id=o.issue_id WHERE i.volume_id=volumes.id
                ) AS issues_downloaded,
                (
                    SELECT COUNT(DISTINCT o.issue_id) FROM canonical_issue_files o
                    JOIN issues i ON i.id=o.issue_id WHERE i.volume_id=volumes.id AND i.monitored=1
                ) AS issues_downloaded_monitored,
                (
                    SELECT SUM(size) FROM (SELECT DISTINCT id, size FROM issues_to_files)
                ) AS total_size
            FROM volumes
            {sql_filter}
            ORDER BY {sort.value}, id
            {paging};
            """, parameters
        ).fetchalldict()

        return volumes

    @classmethod
    def search(
        cls,
        query: str,
        sort: LibrarySorting = LibrarySorting.TITLE,
        filter: Union[LibraryFilter, None] = None
    ) -> List[Dict[str, Any]]:
        """Search in the library with a query.

        Args:
            query (str): The query to search with.

            sort (LibrarySorting, optional): How to sort the list.
                Defaults to LibrarySorting.TITLE.

            filter (Union[LibraryFilters, None], optional): Apply a filter to
                the list if not `None`.
                Defaults to None.

        Returns:
            List[Dict[str, Any]]: The resulting list of matching volumes
                in the library.
        """
        if query.startswith(('4050-', 'cv:')):
            try:
                cv_id = to_number_cv_id((query,))[0]
                volumes = cls.get_public_volumes(sort, cv_id)

            except ValueError:
                volumes = []

        else:
            volumes = [
                v
                for v in cls.get_public_volumes(sort, filter)
                if match_title(v['title'], query, allow_contains=True)
            ]

        return volumes

    @classmethod
    def get_stats(cls) -> Dict[str, int]:
        """Get library statistics.

        Returns:
            Dict[str, int]: The statistics.
        """
        result = get_db().execute("""
            WITH v AS (
                SELECT COUNT(*) AS volumes,
                    SUM(monitored) AS monitored
                FROM volumes
            )
            SELECT
                v.volumes,
                v.monitored,
                v.volumes - v.monitored AS unmonitored,
                (SELECT COUNT(*) FROM issues) AS issues,
                (SELECT COUNT(DISTINCT issue_id) FROM canonical_issue_files) AS downloaded_issues,
                (SELECT COUNT(*) FROM active_files) AS files,
                (SELECT IFNULL(SUM(size), 0) FROM active_files) AS total_file_size
            FROM v;
        """).fetchonedict() or {}
        return result

    @classmethod
    def get_volumes(cls) -> List[int]:
        """Get a list of the IDs of all the volumes.

        Returns:
            List[int]: The list of IDs.
        """
        return first_of_subarrays(get_db().execute(
            "SELECT id FROM volumes;"
        ))

    @classmethod
    def get_volume(cls, volume_id: int) -> Volume:
        """Get a volume from the library.

        Args:
            volume_id (int): The ID of the volume.

        Raises:
            VolumeNotFound: The ID doesn't map to any volume in the library.

        Returns:
            Volume: The volume.
        """
        return Volume(volume_id, check_existence=True)

    @classmethod
    def get_issue(cls, issue_id: int) -> Issue:
        """Get an issue from the library.

        Args:
            issue_id (int): The ID of the issue.

        Raises:
            IssueNotFound: The ID doesn't map to any issue in the library.

        Returns:
            Issue: The issue.
        """
        return Issue(issue_id, check_existence=True)

    @classmethod
    def _cv_to_id(cls, comicvine_id: int) -> Union[int, None]:
        """Find the volume ID based on the CV ID.

        Args:
            comicvine_id (int): The CV ID of the volume to check for.

        Returns:
            bool: The volume ID with the given CV ID, or `None` if not found.
        """
        return get_db().execute(
            "SELECT id FROM volumes WHERE comicvine_id = ? LIMIT 1;",
            (comicvine_id,)
        ).exists()

    @classmethod
    def add(
        cls,
        comicvine_id: int,
        root_folder_id: int,
        monitored: bool,
        monitor_scheme: MonitorScheme = MonitorScheme.ALL,
        monitor_new_issues: bool = True,
        volume_folder: Union[str, None] = None,
        special_version: Union[SpecialVersion, None] = None,
        auto_search: bool = False,
        legacy_default_classification: bool = False
    ) -> int:
        """Legacy ComicVine add contract; all writes use the canonical path."""
        return cls.add_metadata(
            ProviderVolumeIdentity('comicvine', str(comicvine_id)),
            root_folder_id, monitored, monitor_scheme, monitor_new_issues,
            volume_folder, special_version, auto_search,
            legacy_default_classification=legacy_default_classification)

    @classmethod
    def add_metadata(
        cls,
        identity: ProviderVolumeIdentity,
        root_folder_id: int,
        monitored: bool,
        monitor_scheme: MonitorScheme = MonitorScheme.ALL,
        monitor_new_issues: bool = True,
        volume_folder: Union[str, None] = None,
        special_version: Union[SpecialVersion, None] = None,
        auto_search: bool = False,
        organizer_registration: bool = False,
        legacy_default_classification: bool = False
    ) -> int:
        """Add a volume to the library.

        Args:
            comicvine_id (int): The CV ID of the volume.

            root_folder_id (int): The ID of the rootfolder in which
                the volume folder will be.

            monitored (bool): Whether the volume should be monitored.

            monitor_scheme (MonitorScheme, optional): Which issues to monitor.
                Defaults to `MonitorScheme.ALL`.

            monitor_new_issues (bool, optional): Whether to monitor new issues.
                Defaults to True.

            volume_folder (Union[str, None], optional): Custom volume folder.
                Defaults to None.

            special_version (Union[SpecialVersion, None], optional): Give `None`
                to let Kapowarr determine the special version ('auto').
                Otherwise, give a `SpecialVersion` to override and lock the
                special version state.

                Defaults to None.

            auto_search (bool, optional): Start an auto search for the volume
                after adding it.
                Defaults to False.

        Raises:
            RootFolderNotFound: The root folder with the given ID was not found.
            VolumeAlreadyAdded: The volume already exists in the library.
            InvalidKeyValue: The API key of the metadata source is invalid.
            VolumeNotMatched: The ID doesn't map to any volume.
            MetadataSourceRateLimitReached: The metadata source rate limit is
                reached.

        Returns:
            int: The ID of the new volume.
        """
        from backend.implementations.naming import generate_volume_folder_path

        if legacy_default_classification and special_version != SpecialVersion.NORMAL:
            raise ValueError('Legacy default classification requires Normal')

        LOGGER.info(
            'Adding a volume to the library: '
            'metadata %s, RF ID %d, M %s, MS %s, MNI %s, VF %s, SV %s',
            identity,
            root_folder_id,
            monitored,
            monitor_scheme.value,
            monitor_new_issues,
            volume_folder,
            special_version
        )

        if not isinstance(identity.provider_id, str):
            raise ValueError('Provider ID must be a nonempty string')
        if not identity.provider_id:
            raise ValueError('Provider ID must be a nonempty string')
        potential_volume_id = get_db().execute('''SELECT volume_id FROM volume_external_ids
            WHERE provider=? AND provider_id=? LIMIT 1''',
            (identity.provider, identity.provider_id)).exists()
        if potential_volume_id:
            if identity.provider == 'comicvine':
                raise VolumeAlreadyAdded(int(identity.provider_id), potential_volume_id)
            raise InvalidKeyValue('provider_id', identity.provider_id)

        # Raises RootFolderNotFound when ID is invalid
        root_folder = RootFolders().get_one(root_folder_id)

        provider = get_volume_provider(identity.provider)
        fetched = run(fetch_volume_result(provider, identity.provider_id))
        from backend.implementations.metadata.persistence import fetch_input
        vd = fetch_input(fetched, identity.provider)
        if identity.provider != 'comicvine' and vd['provider_id'] != identity.provider_id:
            raise ValueError('Fetched volume identity differs from request')
        validate_format_evidence(fetched.format_evidence, identity.provider, vd['provider_id'])
        validate_publication_evidence(fetched.publication_evidence, identity.provider, vd['provider_id'])
        if (
            fetched.publication_evidence is not None
            and fetched.publication_evidence.publication_kind is not None
        ):
            issues = fetched.metadata.issues
            if (
                issues is None
                or len(issues) != fetched.metadata.issue_count
                or len({i.provider_id for i in issues}) != len(issues)
            ):
                raise MetadataIdentityError('Publication classification requires a complete issue snapshot')
        fetched_at = round(time())

        cursor = get_db()
        with cursor:
            if fetched.snapshot is not None:
                from backend.implementations.metadata.snapshot_persistence import \
                    validate_snapshot_owners
                validate_snapshot_owners(fetched.snapshot)
            volume_id = cursor.execute(
                """
                INSERT INTO volumes(
                    metadata_provider,
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
                    custom_folder,
                    last_cv_fetch,
                    special_version,
                    special_version_locked
                ) VALUES (
                    :metadata_provider,
                    :comicvine_id, :title, :alt_title,
                    :year, :publisher, :volume_number, :description,
                    :site_url, :monitored, :monitor_new_issues,
                    :root_folder, :custom_folder,
                    :last_cv_fetch, :special_version, :special_version_locked
                );
                """,
                {
                    "metadata_provider": identity.provider,
                    "comicvine_id": vd["comicvine_id"],
                    "title": vd["title"],
                    "alt_title": (vd["aliases"] or [None])[0],
                    "year": vd["year"],
                    "publisher": vd["publisher"],
                    "volume_number": vd["volume_number"],
                    "description": vd["description"],
                    "site_url": vd["site_url"],
                    "monitored": monitored,
                    "monitor_new_issues": monitor_new_issues,
                    "root_folder": root_folder.id,
                    "custom_folder": volume_folder is not None,
                    "last_cv_fetch": fetched_at if identity.provider == 'comicvine' else None,
                    "special_version": None,
                    "special_version_locked": special_version is not None
                }
            ).lastrowid

            if identity.provider != 'comicvine':
                ProviderIdentityDB.put_volume_identity(ExternalIdentity(
                    volume_id, identity.provider, vd['provider_id'], 'provider', fetched_at))

            cursor.execute(
                """
                INSERT INTO volumes_covers(volume_id, cover)
                VALUES (:volume_id, :cover);
                """,
                {
                    "volume_id": volume_id,
                    "cover": vd["cover"]
                }
            )

            for i in vd['issues'] or []:
                issue_id = cursor.execute("""
                INSERT INTO issues(
                    volume_id,
                    comicvine_id,
                    issue_number,
                    calculated_issue_number,
                    title,
                    date,
                    description,
                    monitored
                ) VALUES (
                    :volume_id, :comicvine_id,
                    :issue_number, :calculated_issue_number,
                    :title, :date, :description,
                    :monitored
                );
                """,
                    {
                        "volume_id": volume_id,
                        "comicvine_id": i["comicvine_id"],
                        "issue_number": i["issue_number"],
                        "calculated_issue_number": i["calculated_issue_number"],
                        "title": i["title"],
                        "date": i["date"],
                        "description": i["description"],
                        "monitored": True
                    }
                ).lastrowid
                if identity.provider != 'comicvine':
                    ProviderIdentityDB.put_issue_identity(ExternalIdentity(
                        issue_id, identity.provider, i['provider_id'], 'provider'))

            from backend.internals.issue_facts import mapped_facts, write_facts
            owners = dict(cursor.execute('''SELECT e.provider_id,e.issue_id FROM issue_external_ids e
                JOIN issues i ON i.id=e.issue_id WHERE i.volume_id=? AND e.provider=?''',
                (volume_id, identity.provider)))
            for i in (vd['issues'] or []) if fetched.snapshot is None else []:
                write_facts(cursor, owners[i['provider_id']], mapped_facts(
                    i['issue_number'], i['date'], identity.provider + '_mapped'))
            persist_enrichment(fetched, volume_id)
            volume = Volume(volume_id)

            from backend.internals.classification_provenance import (
                ApplicationKind, apply, control, revisions)
            if special_version is None:
                expected = revisions(cursor, [volume_id])[volume_id]
                candidate = evaluate_volume_classification(
                    volume.id, fetched.format_evidence, fetched.publication_evidence)
                apply(cursor, volume_id, candidate.value, decision=candidate, expected_revision=expected)
            else:
                apply(cursor, volume_id, special_version, kind=ApplicationKind.LEGACY_DEFAULT
                      if legacy_default_classification else ApplicationKind.EXPLICIT)
                control(cursor, volume_id, True, 'legacy_default_add' if legacy_default_classification else 'explicit_add')

            # Canonical import registers metadata first. FolderPolicy and the
            # journaled executor subsequently establish the actual folder.
            folder = '' if organizer_registration else generate_volume_folder_path(
                root_folder.folder, volume.get_data(), volume_folder)
            volume.update({'folder': folder})

            if fetched.snapshot is None and not organizer_registration and Settings().sv.create_empty_volume_folders:
                create_folder(folder)
                scan_files(volume_id)

            volume.apply_monitor_scheme(monitor_scheme)

            if fetched.snapshot is None and not organizer_registration:
                mass_process_files(volume_id)

        if auto_search:
            from backend.features.tasks import AutoSearchVolume, TaskHandler

            # Volume is accessed from different thread so changes must be saved,
            # but that's already done by the completion of the transaction above
            task = AutoSearchVolume(volume_id)
            TaskHandler().add(task)

        LOGGER.info(
            f'Added volume with metadata identity {identity} and ID {volume_id}'
        )
        return volume_id


# region Refresh & Scan
def determine_special_version(
    volume_id: int, format_evidence: Union[ProviderFormatEvidence, None] = None,
    publication_evidence: Union[ProviderPublicationEvidence, None] = None
) -> SpecialVersion:
    """Determine what Special Version a volume is, if any.

    Args:
        volume_id (int): The ID of the volume to determine for.
        format_evidence: Optional evidence already validated against the
            selected fetch identity. Locks and VAI take precedence; physical
            hints apply only to a single local issue. Omission retains the
            historical heuristic behavior.
        publication_evidence: Independently validated publication intent/scope.
            Recognized binding plus publication is ambiguous in SpecialVersion;
            neither hint wins and the existing heuristics remain the fallback.

    Returns:
        SpecialVersion: The result.
    """
    return evaluate_volume_classification(volume_id, format_evidence, publication_evidence).value


def evaluate_volume_classification(volume_id, format_evidence=None, publication_evidence=None):
    """Same compatibility loader and clock, returning a candidate only."""
    volume = Volume(volume_id)
    volume_data = volume.get_data()
    from backend.base.definitions import UnsupportedLegacyIssue
    try:
        issues = volume.get_issues()
        classification_issues = tuple(ClassificationIssue(issue.title, issue.date) for issue in issues)
    except UnsupportedLegacyIssue:
        # Rich-only rows deliberately refuse the legacy DTO, but classification
        # needs titles and exact days, not a numeric compatibility projection.
        from backend.internals.issue_facts import load_records
        cursor = get_db()
        records = load_records(cursor, volume_id=volume_id)
        titles = dict(cursor.execute('SELECT id,title FROM issues WHERE volume_id=?', (volume_id,)))
        classification_issues = tuple(ClassificationIssue(titles[r.id],
            r.facts.operational_date.exact_day.isoformat()
            if r.facts and r.facts.operational_date and r.facts.operational_date.exact_day else None)
            for r in records)
    return evaluate_special_version(
        title=volume_data.title, description=volume_data.description,
        issues=classification_issues,
        stored_value=volume_data.special_version,
        locked=volume_data.special_version_locked,
        evaluated_at=datetime.now(), format_evidence=format_evidence,
        publication_evidence=publication_evidence
    )


def refresh_and_scan(
    volume_id: Union[int, None] = None,
    update_websocket: bool = False,
    allow_skipping: bool = True
) -> None:
    """Refresh and scan one or more volumes, which means to pull metadata from
    the online database and to scan for files.

    Args:
        volume_id (Union[int, None], optional): The ID of the volume if it is
            desired to only refresh and scan one. If left to `None`, all volumes
            are refreshed and scanned.
            Defaults to None.

        update_websocket (bool, optional): Send task progress updates over
            the websocket.
            Defaults to False.

        allow_skipping (bool, optional): Skip volumes that have been updated in
            the last 24 hours or that have the same amount of issues as what
            the metadata source reports.
            Defaults to True.

    Raises:
        InvalidKeyValue: The API key of the metadata source is invalid.
    """
    current_time = datetime.now()
    one_day_ago = current_time - ONE_DAY
    cutoff = (one_day_ago if allow_skipping else current_time).timestamp()
    groups: Dict[str, Dict[str, Tuple[int, Union[float, None]]]] = {}
    for identity in ProviderIdentityDB.resolve_volume_metadata_identities(
        PROVIDERS.keys(), volume_id or None
    ):
        if not volume_id and (
            identity.last_fetch is None or identity.last_fetch > cutoff
        ):
            continue
        # Preserve legacy duplicate collapse: last selected local row wins;
        # first occurrence determines request ordering.
        groups.setdefault(identity.provider, {})[identity.provider_id] = (
            identity.local_id, identity.last_fetch)
    for provider_key, identities in groups.items():
        capability = PROVIDERS[provider_key]()
        from backend.implementations.metadata.snapshot import \
            MetadataSnapshotProvider
        if isinstance(capability, (MetadataVolumeProvider, MetadataSnapshotProvider)) and not isinstance(capability, MetadataBulkVolumeProvider):
            from backend.implementations.metadata.refresh import \
                refresh_single_group
            refresh_single_group(capability, provider_key, identities,
                                 current_time, volume_id, update_websocket)
            continue
        _refresh_provider_group(provider_key, identities, current_time,
                                volume_id, update_websocket, allow_skipping)


def _refresh_provider_group(
    provider_key: str,
    identities: Dict[str, Tuple[int, Union[float, None]]],
    current_time: datetime,
    volume_id: Union[int, None],
    update_websocket: bool,
    allow_skipping: bool
) -> None:
    from backend.internals.provider_authority import (RefreshStages,
                                                      capture_refresh)
    tokens = capture_refresh(get_db(), provider_key, identities)
    with RefreshStages(get_db(), tokens.values(), commit) as stages:
        _refresh_provider_group_stages(provider_key, identities, current_time,
            volume_id, update_websocket, allow_skipping, tokens, stages)


def _refresh_provider_group_stages(provider_key, identities, current_time,
                                   volume_id, update_websocket, allow_skipping,
                                   tokens, stages):
    """Existing two-stage reconciliation, dispatched by persisted identity."""
    cursor = get_db()
    from backend.internals.classification_provenance import (InputScope, apply,
                                                             revisions,
                                                             transaction)
    expected_revisions = revisions(cursor, [v[0] for v in identities.values()])
    one_day_ago = current_time - ONE_DAY
    thirty_days_ago = current_time - THIRTY_DAYS

    # Update volumes
    provider = get_bulk_volume_provider(provider_key)
    volume_datas = filtered_volume_datas = [
        volume_input(volume, provider_key)
        for volume in run(provider.fetch_volumes(tuple(identities)))
    ]

    # Authority-sensitive skip/count reads belong to the same guarded stage as
    # the following writes; a switch during acquisition must fail as stale first.
    stages.begin()
    if not volume_id and allow_skipping:
        provider_id_to_issue_count: Dict[str, int] = dict(cursor.execute("""
            SELECT e.provider_id, COUNT(i.id)
            FROM volumes v
            JOIN volume_external_ids e
            ON e.volume_id=v.id AND e.provider=v.metadata_provider
            LEFT JOIN issues i
            ON v.id = i.volume_id
            WHERE e.last_fetch <= ? AND e.provider = ?
            GROUP BY v.id;
            """,
            (one_day_ago.timestamp(), provider_key)
        ))

        filtered_volume_datas = [
            v
            for v in volume_datas
            if provider_id_to_issue_count[v["provider_id"]] != v["issue_count"]
            # Do a fetch anyway if it hasn't been done for 30 days
            or (identities[v["provider_id"]][1] or 0) <= thirty_days_ago.timestamp()
        ]

    cursor.executemany(
        """
        UPDATE volumes
        SET
            title = :title,
            alt_title = :alt_title,
            year = :year,
            publisher = :publisher,
            volume_number = :volume_number,
            description = :description,
            site_url = :site_url,
            last_cv_fetch = CASE WHEN :provider='comicvine' THEN :last_cv_fetch ELSE last_cv_fetch END
        WHERE id = :id;
        """,
        ({
            "provider": provider_key,
            "title": vd["title"],
            "alt_title": (vd["aliases"] or [None])[0],
            "year": vd["year"],
            "publisher": vd["publisher"],
            "volume_number": vd["volume_number"],
            "description": vd["description"],
            "site_url": vd["site_url"],
            "last_cv_fetch": current_time.timestamp(),

            "id": identities[vd["provider_id"]][0]
        }
            for vd in volume_datas
        ))

    cursor.executemany(
        """
        UPDATE volumes_covers
        SET
            cover = :cover
        WHERE volume_id = :volume_id;
        """,
        ({
            "volume_id": identities[vd["provider_id"]][0],
            "cover": vd["cover"]
        }
            for vd in volume_datas
        ))

    if provider_key != 'comicvine':
        cursor.executemany('''UPDATE volume_external_ids SET last_fetch=?
            WHERE volume_id=? AND provider=?''', (
            (current_time.timestamp(), identities[vd['provider_id']][0], provider_key)
            for vd in volume_datas))
    stages.finish()

    # Update issues
    issue_provider_ids = tuple(vd["provider_id"] for vd in filtered_volume_datas)
    issue_datas = [
        issue_input(issue, provider_key)
        for issue in run(provider.fetch_issues(issue_provider_ids))
    ]
    stages.begin()
    monitor_issues_volume_ids: Set[int] = set(first_of_subarrays(cursor.execute(
        "SELECT id FROM volumes WHERE monitor_new_issues = 1;"
    )))
    ProviderIdentityDB.validate_issue_metadata_identities(provider_key)
    write_issue_batch(cursor,
        """
        INSERT INTO issues(
            id,
            volume_id,
            comicvine_id,
            issue_number,
            calculated_issue_number,
            title,
            date,
            description,
            monitored
        ) VALUES (
            (SELECT e.issue_id FROM issue_external_ids e
             JOIN issues existing ON existing.id=e.issue_id
             JOIN volumes owner ON owner.id=existing.volume_id
             WHERE e.provider=:provider AND e.provider_id=:provider_id
                 AND owner.metadata_provider=:provider),
            :volume_id, :comicvine_id, :issue_number, :calculated_issue_number,
            :title, :date, :description, :monitored
        )
        ON CONFLICT(id) DO
        UPDATE
        SET
            issue_number = :issue_number,
            calculated_issue_number = :calculated_issue_number,
            title = :title,
            date = :date,
            description = :description;
        """,
        ({
            "provider": provider_key,
            "provider_id": isd["provider_id"],
            "volume_id": identities[isd["volume_provider_id"]][0],
            "comicvine_id": isd["comicvine_id"],
            "issue_number": isd["issue_number"],
            "calculated_issue_number": isd["calculated_issue_number"] or 0.0,
            "title": isd["title"],
            "date": isd["date"],
            "description": isd["description"],
            "monitored": identities[isd["volume_provider_id"]][0] in monitor_issues_volume_ids
        }
            for isd in issue_datas
        ), provider_key)

    stages.finish()

    # Delete issues from DB that aren't found in response
    volume_issues_fetched: Dict[str, Set[str]] = {}
    for isd in issue_datas:
        (volume_issues_fetched
            .setdefault(isd["volume_provider_id"], set())
            .add(isd["provider_id"]))

    stages.begin()
    for vd in filtered_volume_datas:
        if len(volume_issues_fetched.get(
            vd["provider_id"]
        ) or tuple()) != vd["issue_count"]:
            continue

        # All issues of the volume have been fetched, which is not guaranteed
        # because of rate limits.
        issue_provider_to_id = dict(cursor.execute("""
            SELECT ie.provider_id, i.id
            FROM issues i
            INNER JOIN volumes v
            ON i.volume_id = v.id
            JOIN volume_external_ids ve ON ve.volume_id=v.id
                AND ve.provider=v.metadata_provider
            JOIN issue_external_ids ie ON ie.issue_id=i.id
                AND ie.provider=v.metadata_provider
            WHERE ve.provider=? AND ve.provider_id=?;
            """,
            (provider_key, vd["provider_id"])
        ).fetchall())
        if issue_provider_to_id and vd["provider_id"] not in volume_issues_fetched:
            # Preserve the existing empty-result CV KeyError, including its ID.
            raise KeyError(int(vd["provider_id"]) if provider_key == 'comicvine'
                           else vd["provider_id"])
        for issue_provider_id, issue_id in issue_provider_to_id.items():
            if issue_provider_id not in volume_issues_fetched[vd["provider_id"]]:
                # Issue is in database but not in response, so remove
                Issue(issue_id).delete()
                stages.finish()
                stages.begin()
    stages.finish(False)

    # Refresh Special Version
    # Preserve the old final classification-only commit boundary, not the
    # preceding metadata/issue partial commits. No per-volume receipt reads.
    stages.begin()
    with transaction(cursor):
        updated_special_versions = tuple(
            (identities[vd['provider_id']][0], evaluate_volume_classification(identities[vd['provider_id']][0]))
            for vd in volume_datas)
        for local, candidate in updated_special_versions:
            apply(cursor, local, candidate.value, decision=candidate, scope=InputScope.DURABLE,
                  expected_revision=expected_revisions[local])

    stages.finish()

    # Scan for files
    if volume_id:
        scan_files(volume_id, update_websocket=update_websocket,
                   expected_authority=tokens[volume_id])

    else:
        v_ids = [
            (v[0], [], False, update_websocket, tokens[v[0]])
            for v in identities.values()
        ]
        total_count = len(v_ids)

        if not total_count:
            return

        with PortablePool(max_processes=min(
            Constants.DB_MAX_CONCURRENT_CONNECTIONS,
            total_count
        )) as pool:
            if update_websocket:
                ws = WebSocket()
                for idx, _ in enumerate(
                    pool.istarmap_unordered(scan_files, v_ids)
                ):
                    ws.emit(TaskStatusEvent(
                        f'Scanned files for volume {idx+1}/{total_count}'
                    ))

            else:
                pool.starmap(scan_files, v_ids)

        stages.begin()
        from backend.internals.scan_mutation import unmatched_reserved
        if unmatched_reserved(cursor):
            LOGGER.warning('Refresh orphan pruning deferred: organization reservation')
        else:
            FilesDB.delete_unmatched_files()
        stages.finish(False)

    return


def delete_issue_file(file_id: int) -> None:
    """Delete a file from the library and remove it from the filesystem.

    Args:
        file_id (int): The ID of the file to delete.
    """
    with deletion_mutation(get_db(), file_id=file_id):
        _delete_issue_file_guarded(file_id)


def _delete_issue_file_guarded(file_id: int) -> None:
    file_data = FilesDB.fetch(file_id=file_id)[0]
    volume_id = FilesDB.volume_of_file(file_data["filepath"])
    unmonitor_deleted_issues = Settings().sv.unmonitor_deleted_issues and volume_id

    if volume_id:
        vf = Library.get_volume(volume_id).vd.folder
        delete_file_folder(file_data["filepath"])
        delete_empty_parent_folders(dirname(file_data["filepath"]), vf)
    else:
        delete_file_folder(file_data["filepath"])

    cursor = get_db()
    not_downloaded_issues: List[int] = first_of_subarrays(cursor.execute("""
        WITH matched_file_counts AS (
            SELECT
                issue_id,
                COUNT(DISTINCT file_id) AS matched_file_count
            FROM canonical_issue_files
            WHERE issue_id IN (
                SELECT issue_id
                FROM issues_files
                WHERE file_id = ?
            )
            GROUP BY issue_id
        )
        SELECT issue_id
        FROM matched_file_counts
        WHERE matched_file_count = 1;
        """,
        (file_id,)
    ))

    if volume_id:
        WebSocket().emit(DownloadedStatusEvent(
            volume_id,
            not_downloaded_issues=not_downloaded_issues
        ))

    if unmonitor_deleted_issues:
        cursor.executemany(
            "UPDATE issues SET monitored = 0 WHERE id = ?;",
            ((i,) for i in not_downloaded_issues)
        )

    FilesDB.delete_file(file_id)

    return
