"""Frozen schema-52 refresh from 15d6cee, for runtime-cutover parity only."""

LEGACY_REFRESH_SOURCE = r'''
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
    thirty_days_ago = current_time - THIRTY_DAYS

    cursor = get_db()
    if volume_id:
        cursor.execute("""
            SELECT comicvine_id, id, last_cv_fetch
            FROM volumes
            WHERE id = ?
            LIMIT 1;
            """,
            (volume_id,)
        )

    else:
        cursor.execute("""
            SELECT comicvine_id, id, last_cv_fetch
            FROM volumes
            WHERE last_cv_fetch <= ?
            ORDER BY last_cv_fetch ASC;
            """,
            (
                one_day_ago.timestamp()
                if allow_skipping else
                current_time.timestamp(),
            )
        )

    cv_to_id_fetch: Dict[int, Tuple[int, int]] = {
        e["comicvine_id"]: (e["id"], e["last_cv_fetch"])
        for e in cursor
    }
    if not cv_to_id_fetch:
        return

    # Update volumes
    provider_key, provider_ids = legacy_volume_identities(
        tuple(cv_to_id_fetch.keys())
    )
    provider = get_bulk_volume_provider(provider_key)
    volume_datas = filtered_volume_datas = [
        legacy_volume_metadata(volume)
        for volume in run(provider.fetch_volumes(provider_ids))
    ]

    if not volume_id and allow_skipping:
        cv_id_to_issue_count: Dict[int, int] = dict(cursor.execute("""
            SELECT v.comicvine_id, COUNT(i.id)
            FROM volumes v
            LEFT JOIN issues i
            ON v.id = i.volume_id
            WHERE v.last_cv_fetch <= ?
            GROUP BY v.id;
            """,
            (one_day_ago.timestamp(),)
        ))

        filtered_volume_datas = [
            v
            for v in volume_datas
            if cv_id_to_issue_count[v["comicvine_id"]] != v["issue_count"]
            # Do a fetch anyway if it hasn't been done for 30 days
            or cv_to_id_fetch[v["comicvine_id"]][1] <= thirty_days_ago.timestamp()
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
            last_cv_fetch = :last_cv_fetch
        WHERE id = :id;
        """,
        ({
            "title": vd["title"],
            "alt_title": (vd["aliases"] or [None])[0],
            "year": vd["year"],
            "publisher": vd["publisher"],
            "volume_number": vd["volume_number"],
            "description": vd["description"],
            "site_url": vd["site_url"],
            "last_cv_fetch": current_time.timestamp(),

            "id": cv_to_id_fetch[vd["comicvine_id"]][0]
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
            "volume_id": cv_to_id_fetch[vd["comicvine_id"]][0],
            "cover": vd["cover"]
        }
            for vd in volume_datas
        ))

    commit()

    # Update issues
    _, issue_provider_ids = legacy_volume_identities(
        tuple(vd["comicvine_id"] for vd in filtered_volume_datas)
    )
    issue_datas = [
        legacy_issue_metadata(issue)
        for issue in run(provider.fetch_issues(issue_provider_ids))
    ]
    monitor_issues_volume_ids: Set[int] = set(first_of_subarrays(cursor.execute(
        "SELECT id FROM volumes WHERE monitor_new_issues = 1;"
    )))
    cursor.executemany(
        """
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
            :volume_id, :comicvine_id, :issue_number, :calculated_issue_number,
            :title, :date, :description, :monitored
        )
        ON CONFLICT(comicvine_id) DO
        UPDATE
        SET
            issue_number = :issue_number,
            calculated_issue_number = :calculated_issue_number,
            title = :title,
            date = :date,
            description = :description;
        """,
        ({
            "volume_id": cv_to_id_fetch[isd["volume_id"]][0],
            "comicvine_id": isd["comicvine_id"],
            "issue_number": isd["issue_number"],
            "calculated_issue_number": isd["calculated_issue_number"] or 0.0,
            "title": isd["title"],
            "date": isd["date"],
            "description": isd["description"],
            "monitored": cv_to_id_fetch[isd["volume_id"]][0] in monitor_issues_volume_ids
        }
            for isd in issue_datas
        ))

    commit()

    # Delete issues from DB that aren't found in response
    volume_issues_fetched: Dict[int, Set[int]] = {}
    for isd in issue_datas:
        (volume_issues_fetched
            .setdefault(isd["volume_id"], set())
            .add(isd["comicvine_id"]))

    for vd in filtered_volume_datas:
        if len(volume_issues_fetched.get(
            vd["comicvine_id"]
        ) or tuple()) != vd["issue_count"]:
            continue

        # All issues of the volume have been fetched, which is not guaranteed
        # because of rate limits.
        issue_cv_to_id = dict(cursor.execute("""
            SELECT i.comicvine_id, i.id
            FROM issues i
            INNER JOIN volumes v
            ON i.volume_id = v.id
            WHERE v.comicvine_id = ?;
            """,
            (vd["comicvine_id"],)
        ).fetchall())
        for issue_cv, issue_id in issue_cv_to_id.items():
            if issue_cv not in volume_issues_fetched[vd["comicvine_id"]]:
                # Issue is in database but not in response, so remove
                Issue(issue_id).delete()
                commit()

    # Refresh Special Version
    updated_special_versions = tuple(
        {
            "special_version": determine_special_version(
                cv_to_id_fetch[vd["comicvine_id"]][0]
            ),
            "id": cv_to_id_fetch[vd["comicvine_id"]][0]
        }
        for vd in volume_datas
    )
    cursor.executemany("""
        UPDATE volumes
        SET special_version = :special_version
        WHERE id = :id AND special_version_locked = 0;
        """,
        updated_special_versions
    )

    commit()

    # Scan for files
    if volume_id:
        scan_files(volume_id, update_websocket=update_websocket)

    else:
        v_ids = [
            (v[0], [], False, update_websocket)
            for v in cv_to_id_fetch.values()
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

        FilesDB.delete_unmatched_files()

    return
'''
