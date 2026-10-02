"""Frozen schema-51 builder and logical receipt; no runtime DB binaries."""

from pathlib import Path

SCHEMA_51 = Path(__file__).with_name(
    'schema_51.sql').read_text(encoding='utf-8')


def build_legacy(db, volumes=3, issues_per_volume=3, edges=True):
    db.executescript(SCHEMA_51)
    db.execute("INSERT INTO config VALUES ('database_version',51)")
    db.execute("INSERT INTO config VALUES ('api_key','test-only-migration-key')")
    db.execute("INSERT INTO root_folders VALUES (7,'/comics/')")
    db.executemany('''INSERT INTO volumes
        (id,comicvine_id,title,alt_title,root_folder,folder,last_cv_fetch,
         monitored,monitor_new_issues,special_version,special_version_locked)
        VALUES (?,?,?,?,7,?,?,?,?,?,?)''', (
        (v * 10, 42 if edges and v <= 2 else
         (9223372036854775807 if edges and v == 3 else v),
         'Synthetic ' + str(v), 'Alias é', '/comics/synthetic-' + str(v),
         None if edges and v == 1 else
         (9223372036854775807 if edges and v == 3 else 1234567890.25),
         v % 2, (v + 1) % 2, 'tpb' if v == 2 else None, v == 2)
        for v in range(1, volumes + 1)))
    db.executemany('''INSERT INTO issues
        (id,volume_id,comicvine_id,issue_number,calculated_issue_number,
         title,date,description,monitored) VALUES (?,?,?,?,?,?,?,?,?)''', (
        (n * 10, ((n - 1) // issues_per_volume + 1) * 10,
         9223372036854775807 if edges and n == 1 else
         (-9223372036854775808 if edges and n == 2 else n),
         '1/2' if n == 1 else str(n), n / 2, 'TPB' if n == 1 else None,
         None, '<p>Unchanged metadata</p>', n % 2)
        for n in range(1, volumes * issues_per_volume + 1)))
    if volumes and issues_per_volume >= 2:
        db.executescript('''
            INSERT INTO volumes_covers VALUES (10,X'000102FF');
            INSERT INTO files VALUES (8,'/comics/synthetic.cbz',123);
            INSERT INTO files VALUES (9,'/comics/cover.jpg',456);
            INSERT INTO files VALUES (11,'/comics/unlinked.cbz',789);
            INSERT INTO issues_files VALUES (8,10,1),(8,20,0);
            INSERT INTO volume_files VALUES (9,10,'cover',1);
            INSERT INTO download_history(volume_id,issue_id,downloaded_at)
                VALUES (10,10,123),(NULL,NULL,124);
            INSERT INTO blocklist(id,volume_id,issue_id,reason,added_at)
                VALUES (3,10,20,1,123);
            INSERT INTO download_queue
                (id,volume_id,client_type,download_link,source_type,source_name)
                VALUES (4,10,'test','https://example.invalid','test','test');
        ''')
    db.commit()


def receipt(db, columns=None):
    if columns is None:
        columns = {name: [r[1] for r in db.execute(
            'PRAGMA table_info("' + name + '")')]
            for (name,) in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}
    rows = {}
    for table, names in columns.items():
        projection = ','.join('"' + name + '"' for name in names)
        rows[table] = db.execute(
            'SELECT ' + projection + ' FROM "' + table + '" ORDER BY rowid'
        ).fetchall()
    rows['config'] = [r for r in rows['config'] if r[0] != 'database_version']
    return columns, rows
