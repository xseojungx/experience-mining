CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'complete')),
    approval_id TEXT REFERENCES approvals(id)
);
CREATE TABLE IF NOT EXISTS checkpoints (
    seq INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_id TEXT NOT NULL REFERENCES work_items(id),
    next_id TEXT REFERENCES work_items(id),
    details TEXT NOT NULL
);

-- A future collection approval explicitly identifies its project, repository/export
-- and operation. A local implementation approval alone grants no queue access.
CREATE TABLE IF NOT EXISTS queue_scopes (
    approval_id TEXT NOT NULL REFERENCES approvals(id),
    project TEXT NOT NULL,
    origin TEXT NOT NULL,
    operation TEXT NOT NULL,
    PRIMARY KEY (approval_id, project, origin, operation)
);
CREATE TABLE IF NOT EXISTS queue_units (
    id INTEGER PRIMARY KEY,
    approval_id TEXT NOT NULL,
    project TEXT NOT NULL,
    origin TEXT NOT NULL,
    operation TEXT NOT NULL,
    unit_key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'complete')),
    token TEXT,
    lease_until INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    result TEXT,
    UNIQUE (approval_id, project, origin, operation, unit_key),
    FOREIGN KEY (approval_id, project, origin, operation)
        REFERENCES queue_scopes(approval_id, project, origin, operation)
);
CREATE TABLE IF NOT EXISTS source_versions (
    id INTEGER PRIMARY KEY,
    origin TEXT NOT NULL,
    source_key TEXT NOT NULL,
    location TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    registered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (origin, source_key, sha256)
);
CREATE TABLE IF NOT EXISTS unit_sources (
    unit_id INTEGER NOT NULL REFERENCES queue_units(id),
    version_id INTEGER NOT NULL REFERENCES source_versions(id),
    PRIMARY KEY (unit_id, version_id)
);
