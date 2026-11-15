-- Catalog holds title-only decisions, including NON_DEV; no message text.
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS accounts(
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, physical_path TEXT NOT NULL UNIQUE,
 logical_account TEXT CHECK(logical_account IS NULL OR logical_account IN ('A','B'))
);
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,account TEXT NOT NULL REFERENCES accounts(id),file_set TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files(
 id TEXT PRIMARY KEY,account TEXT NOT NULL REFERENCES accounts(id),batch TEXT NOT NULL REFERENCES batches(id),
 path TEXT NOT NULL,relative_path TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,kind TEXT NOT NULL,
 UNIQUE(batch,path)
);
CREATE TABLE IF NOT EXISTS classifications(
 id TEXT PRIMARY KEY,account TEXT NOT NULL REFERENCES accounts(id),file_id TEXT NOT NULL REFERENCES files(id),
 conversation_id TEXT NOT NULL,id_kind TEXT NOT NULL,pointer TEXT NOT NULL,title TEXT NOT NULL,
 category TEXT NOT NULL CHECK(category IN ('DEV','AMBIGUOUS','NON_DEV')),
 reason TEXT NOT NULL,rule TEXT NOT NULL,needs_review INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('excluded','pending_body','processed','held')),
 UNIQUE(file_id,pointer,rule)
);
CREATE TABLE IF NOT EXISTS stages(
 file_id TEXT NOT NULL REFERENCES files(id),stage TEXT NOT NULL,version TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('complete','failed')),count INTEGER NOT NULL DEFAULT 0,
 error_code TEXT,PRIMARY KEY(file_id,stage,version)
);
