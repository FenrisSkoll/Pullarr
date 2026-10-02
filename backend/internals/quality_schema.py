"""Schema70: opt-in policy and immutable acquisition/file observations."""

STATEMENTS = (
    '''CREATE TABLE quality_profiles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 100),
        revision INTEGER NOT NULL CHECK(revision>=1),
        upgrades INTEGER NOT NULL CHECK(upgrades IN (0,1)),
        cutoff INTEGER NOT NULL CHECK(cutoff BETWEEN 0 AND 6),
        minimum_p10 INTEGER NOT NULL CHECK(minimum_p10 BETWEEN 0 AND 20000)
    )''',
    '''CREATE TABLE quality_groups (
        profile_id INTEGER NOT NULL REFERENCES quality_profiles(id) ON DELETE CASCADE,
        position INTEGER NOT NULL CHECK(position BETWEEN 0 AND 6),
        name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 80),
        allowed INTEGER NOT NULL CHECK(allowed IN (0,1)),
        PRIMARY KEY(profile_id,position)
    )''',
    '''CREATE TABLE quality_classes (
        profile_id INTEGER NOT NULL,
        position INTEGER NOT NULL,
        class TEXT NOT NULL CHECK(class IN ('unknown','digital','hd_digital','sd_digital','scan','upscaled','hd_upscaled')),
        PRIMARY KEY(profile_id,class),
        FOREIGN KEY(profile_id,position) REFERENCES quality_groups(profile_id,position) ON DELETE CASCADE
    )''',
    '''CREATE TABLE quality_default (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
        profile_id INTEGER NOT NULL REFERENCES quality_profiles(id) ON DELETE RESTRICT,
        revision INTEGER NOT NULL DEFAULT 0 CHECK(revision>=0)
    )''',
    '''CREATE TABLE volume_quality_profiles (
        volume_id INTEGER PRIMARY KEY REFERENCES volumes(id) ON DELETE CASCADE,
        profile_id INTEGER NOT NULL REFERENCES quality_profiles(id) ON DELETE RESTRICT
    )''',
    '''CREATE TABLE collection_quality_profiles (
        node_id INTEGER PRIMARY KEY REFERENCES collection_nodes(id) ON DELETE CASCADE,
        profile_id INTEGER NOT NULL REFERENCES quality_profiles(id) ON DELETE RESTRICT
    )''',
    '''CREATE TABLE file_quality_assessments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
        fingerprint TEXT NOT NULL CHECK(length(fingerprint)=64),
        analyzer TEXT NOT NULL,
        facts TEXT NOT NULL CHECK(json_valid(facts) AND length(facts)<=16384),
        observed_at REAL NOT NULL,
        UNIQUE(file_id,fingerprint,analyzer)
    )''',
    'CREATE INDEX quality_file_latest ON file_quality_assessments(file_id,id)',
    '''CREATE TABLE acquisition_provenance (
        id TEXT PRIMARY KEY CHECK(length(id)=32),
        volume_id INTEGER REFERENCES volumes(id) ON DELETE SET NULL,
        issue_id INTEGER REFERENCES issues(id) ON DELETE SET NULL,
        reason TEXT NOT NULL CHECK(reason IN ('missing','upgrade','manual','manual_import','legacy')),
        state TEXT NOT NULL CHECK(state IN ('selected','grabbed','downloaded','verifying','imported','rejected','failed')),
        candidate_key TEXT CHECK(candidate_key IS NULL OR length(candidate_key)=64),
        release_title TEXT NOT NULL CHECK(length(release_title)<=1000),
        source TEXT NOT NULL CHECK(length(source)<=200),
        claims TEXT NOT NULL CHECK(json_valid(claims) AND length(claims)<=4096),
        profile_id INTEGER,
        profile_revision INTEGER,
        profile_snapshot TEXT NOT NULL CHECK(json_valid(profile_snapshot) AND length(profile_snapshot)<=8192),
        decision TEXT NOT NULL CHECK(json_valid(decision) AND length(decision)<=16384),
        client_kind TEXT CHECK(client_kind IS NULL OR client_kind IN ('sabnzbd','direct_download','manual')),
        client_job TEXT CHECK(client_job IS NULL OR length(client_job)<=200),
        file_id INTEGER REFERENCES files(id) ON DELETE SET NULL,
        assessment_id INTEGER REFERENCES file_quality_assessments(id) ON DELETE RESTRICT,
        supersedes TEXT REFERENCES acquisition_provenance(id) ON DELETE RESTRICT,
        error TEXT CHECK(error IS NULL OR length(error)<=100),
        created_at REAL NOT NULL, updated_at REAL NOT NULL,
        CHECK(supersedes IS NULL OR supersedes!=id)
    )''',
    'CREATE INDEX quality_provenance_issue ON acquisition_provenance(issue_id,created_at,id)',
    'CREATE INDEX quality_provenance_file ON acquisition_provenance(file_id,state,created_at)',
    'CREATE INDEX quality_provenance_client ON acquisition_provenance(client_kind,client_job)',
    '''CREATE TABLE quality_rejections (
        issue_id INTEGER NOT NULL REFERENCES issues(id) ON DELETE CASCADE,
        candidate_key TEXT NOT NULL CHECK(length(candidate_key)=64),
        profile_id INTEGER NOT NULL REFERENCES quality_profiles(id) ON DELETE CASCADE,
        profile_revision INTEGER NOT NULL,
        reason TEXT NOT NULL CHECK(reason IN ('dimension_floor_failed','verification_unavailable','integrity_failed','equal','downgrade','identity_failed')),
        created_at REAL NOT NULL,
        PRIMARY KEY(issue_id,candidate_key,profile_id,profile_revision)
    )''',
    "INSERT INTO quality_profiles VALUES(1,'Preserve existing acquisition',1,0,0,0)",
    "INSERT INTO quality_groups VALUES(1,0,'Admitted (no automatic upgrades)',1)",
    "INSERT INTO quality_classes VALUES(1,0,'unknown'),(1,0,'digital'),(1,0,'hd_digital'),(1,0,'sd_digital'),(1,0,'scan'),(1,0,'upscaled'),(1,0,'hd_upscaled')",
    'INSERT INTO quality_default(singleton,profile_id) VALUES(1,1)',
    "ALTER TABLE wanted_decisions ADD COLUMN acquisition_reason TEXT NOT NULL DEFAULT 'missing' CHECK(acquisition_reason IN ('missing','upgrade','manual'))",
    '''CREATE VIEW quality_effective_volumes AS
        WITH RECURSIVE paths(volume_id,node_id,depth) AS (
            SELECT p.local_volume_id,m.node_id,0 FROM collection_publications p
            JOIN collection_memberships m ON m.publication_id=p.id WHERE p.local_volume_id IS NOT NULL
            UNION ALL
            SELECT p.volume_id,n.parent_id,p.depth+1 FROM paths p JOIN collection_nodes n ON n.id=p.node_id
            WHERE n.parent_id IS NOT NULL AND p.depth<7
              AND NOT EXISTS(SELECT 1 FROM collection_quality_profiles q WHERE q.node_id=p.node_id)
        ), inherited AS (
            SELECT p.volume_id,MIN(q.profile_id) profile_id,COUNT(DISTINCT q.profile_id) choices
            FROM paths p JOIN collection_quality_profiles q ON q.node_id=p.node_id GROUP BY p.volume_id
        ) SELECT v.id volume_id,
          CASE WHEN x.profile_id IS NOT NULL THEN x.profile_id WHEN h.choices>1 THEN NULL
               ELSE COALESCE(h.profile_id,d.profile_id) END profile_id,
          x.profile_id IS NULL AND COALESCE(h.choices,0)>1 conflict
        FROM volumes v CROSS JOIN quality_default d LEFT JOIN volume_quality_profiles x ON x.volume_id=v.id
        LEFT JOIN inherited h ON h.volume_id=v.id''',
    '''CREATE VIEW quality_upgrade_issues AS
        SELECT i.id issue_id FROM issues i JOIN volumes v ON v.id=i.volume_id
        JOIN quality_effective_volumes e ON e.volume_id=v.id
        JOIN quality_profiles p ON p.id=e.profile_id
        JOIN issues_files b ON b.issue_id=i.id JOIN active_files f ON f.id=b.file_id
        JOIN file_quality_assessments a ON a.id=(SELECT MAX(a2.id) FROM file_quality_assessments a2 WHERE a2.file_id=f.id)
        LEFT JOIN acquisition_provenance r ON r.id=(SELECT r2.id FROM acquisition_provenance r2
            WHERE r2.file_id=f.id AND r2.state='imported' ORDER BY r2.created_at DESC,r2.id DESC LIMIT 1)
        JOIN quality_classes c ON c.profile_id=p.id AND c.class=COALESCE(json_extract(r.claims,'$.quality_class'),'unknown')
        JOIN quality_groups g ON g.profile_id=c.profile_id AND g.position=c.position
        WHERE i.monitored=1 AND v.monitored=1 AND p.upgrades=1
          AND (SELECT COUNT(*) FROM issues_files b2 JOIN active_files f2 ON f2.id=b2.file_id WHERE b2.issue_id=i.id)=1
          AND (SELECT COUNT(*) FROM issues_files b3 WHERE b3.file_id=f.id)=1
          AND NOT EXISTS(SELECT 1 FROM volume_files vf WHERE vf.file_id=f.id)
          AND NOT EXISTS(SELECT 1 FROM file_content_coverage fc WHERE fc.file_id=f.id AND fc.retired_at IS NULL)
          AND COALESCE(json_extract(r.claims,'$.conflict'),0)=0
          AND json_extract(a.facts,'$.size')=f.size
          AND (c.position<p.cutoff OR g.allowed=0 OR json_extract(a.facts,'$.integrity')!='valid'
               OR (p.minimum_p10>0 AND COALESCE(json_extract(a.facts,'$.short_edge.p10'),0)<p.minimum_p10))''',
)
SCHEMA = ';\n'.join(STATEMENTS) + ';\n'
