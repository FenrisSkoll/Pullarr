-- Frozen DB_SCHEMA at e3fd5ea11032d3fcca42b18c196778ae66cf45d3.
CREATE TABLE IF NOT EXISTS config(
    key VARCHAR(100) PRIMARY KEY,
    value BLOB
);
CREATE TABLE IF NOT EXISTS root_folders(
    id INTEGER PRIMARY KEY,
    folder VARCHAR(254) UNIQUE NOT NULL
);
CREATE TABLE IF NOT EXISTS volumes(
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
CREATE TABLE IF NOT EXISTS volumes_covers(
    volume_id INTEGER UNIQUE NOT NULL,
    cover BLOB,
    FOREIGN KEY (volume_id) REFERENCES volumes(id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS volumes_covers_volume_id_index
    ON volumes_covers(volume_id);
CREATE TABLE IF NOT EXISTS issues(
    id INTEGER PRIMARY KEY,
    volume_id INTEGER NOT NULL,
    comicvine_id INTEGER NOT NULL UNIQUE,
    issue_number VARCHAR(20) NOT NULL,
    calculated_issue_number FLOAT(20) NOT NULL,
    title VARCHAR(255),
    date VARCHAR(10),
    description TEXT,
    monitored BOOL NOT NULL DEFAULT 1,

    FOREIGN KEY (volume_id) REFERENCES volumes(id)
        ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS issues_volume_number_index
    ON issues(volume_id, calculated_issue_number);
CREATE INDEX IF NOT EXISTS issues_volume_index
    ON issues(volume_id);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    filepath TEXT UNIQUE NOT NULL,
    size INTEGER
);
CREATE TABLE IF NOT EXISTS issues_files(
    file_id INTEGER NOT NULL,
    issue_id INTEGER NOT NULL,
    forced BOOL NOT NULL DEFAULT 0,

    FOREIGN KEY (file_id) REFERENCES files(id)
        ON DELETE CASCADE,
    FOREIGN KEY (issue_id) REFERENCES issues(id),
    CONSTRAINT PK_issues_files PRIMARY KEY (
        file_id,
        issue_id
    )
);
CREATE INDEX IF NOT EXISTS issues_files_issue_id_index
    ON issues_files(issue_id);
CREATE TABLE IF NOT EXISTS volume_files(
    file_id INTEGER PRIMARY KEY,
    volume_id INTEGER NOT NULL,
    file_type VARCHAR(15) NOT NULL,
    forced BOOL NOT NULL DEFAULT 0,

    FOREIGN KEY (volume_id) REFERENCES volumes(id)
        ON DELETE CASCADE,
    FOREIGN KEY (file_id) REFERENCES files(id)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS indexer_clients(
    id INTEGER PRIMARY KEY,
    enabled BOOL NOT NULL DEFAULT 1,
    download_type INTEGER NOT NULL,
    client_type VARCHAR(255) NOT NULL,
    title VARCHAR(255) NOT NULL,
    url TEXT NOT NULL,

    gc_service_preference TEXT,
    gc_avoid_large_downloads BOOL
);
CREATE TABLE IF NOT EXISTS external_download_clients(
    id INTEGER PRIMARY KEY,
    enabled BOOL NOT NULL DEFAULT 1,
    download_type INTEGER NOT NULL,
    client_type VARCHAR(255) NOT NULL,
    title VARCHAR(255) NOT NULL,
    base_url TEXT NOT NULL,
    username VARCHAR(255),
    password VARCHAR(255),
    api_token VARCHAR(255)
);
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
CREATE TABLE IF NOT EXISTS download_history(
    web_link TEXT,
    web_title TEXT,
    web_sub_title TEXT,
    file_title TEXT,

    volume_id INTEGER,
    issue_id INTEGER,

    source VARCHAR(25),
    downloaded_at INTEGER NOT NULL CHECK (downloaded_at > 0),
    success BOOL,

    FOREIGN KEY (volume_id) REFERENCES volumes(id)
        ON DELETE SET NULL,
    FOREIGN KEY (issue_id) REFERENCES issues(id)
        ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS task_history(
    task_name NOT NULL,
    display_title NOT NULL,
    run_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_intervals(
    task_name PRIMARY KEY,
    schedule TEXT NOT NULL,
    next_run INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS blocklist(
    id INTEGER PRIMARY KEY,
    volume_id INTEGER,
    issue_id INTEGER,

    web_link TEXT,
    web_title TEXT,
    web_sub_title TEXT,

    download_link TEXT UNIQUE,
    download_service VARCHAR(30),

    reason INTEGER NOT NULL CHECK (reason > 0),
    added_at INTEGER NOT NULL CHECK (added_at > 0),

    FOREIGN KEY (volume_id) REFERENCES volumes(id)
        ON DELETE SET NULL,
    FOREIGN KEY (issue_id) REFERENCES issues(id)
        ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS credentials(
    id INTEGER PRIMARY KEY,
    source VARCHAR(30) NOT NULL,
    username TEXT,
    email TEXT,
    password TEXT,
    api_key TEXT
);
CREATE TABLE IF NOT EXISTS remote_mappings(
    id INTEGER PRIMARY KEY,
    external_download_client_id INTEGER NOT NULL,
    remote_path TEXT NOT NULL,
    local_path TEXT NOT NULL,

    FOREIGN KEY (external_download_client_id)
        REFERENCES external_download_clients(id)
        ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS status(
    status_type VARCHAR(100) NOT NULL,
    subtype VARCHAR(100) NOT NULL,
    timestamp INTEGER NOT NULL,
    expires_at INTEGER,
    PRIMARY KEY (status_type, subtype)
);
