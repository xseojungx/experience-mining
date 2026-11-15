-- Additive execution controls; ana_migrations remains schema version 2.
CREATE TABLE ctl_migrations(version INTEGER PRIMARY KEY CHECK(version=1), sha256 TEXT NOT NULL);
CREATE TABLE ctl_grants(
 id TEXT PRIMARY KEY, approval_id TEXT NOT NULL REFERENCES approvals(id),
 approval_digest TEXT NOT NULL, source TEXT NOT NULL, scope TEXT NOT NULL CHECK(json_valid(scope)),
 valid_from INTEGER NOT NULL, valid_until INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1))
);
CREATE TABLE ctl_jobs(
 unit_id INTEGER PRIMARY KEY REFERENCES queue_units(id), grant_id TEXT NOT NULL REFERENCES ctl_grants(id),
 contract TEXT NOT NULL CHECK(json_valid(contract)), contract_digest TEXT NOT NULL,
 disposition TEXT NOT NULL DEFAULT 'active' CHECK(disposition IN ('active','held','failed')),
 resume_condition TEXT, next_attempt INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL CHECK(max_attempts>0)
);
CREATE TABLE ctl_dependencies(
 unit_id INTEGER NOT NULL REFERENCES ctl_jobs(unit_id), predecessor INTEGER NOT NULL REFERENCES ctl_jobs(unit_id),
 PRIMARY KEY(unit_id,predecessor), CHECK(unit_id<>predecessor)
);
CREATE TABLE ctl_events(
 seq INTEGER PRIMARY KEY, unit_id INTEGER NOT NULL REFERENCES ctl_jobs(unit_id),
 event TEXT NOT NULL, details TEXT NOT NULL CHECK(json_valid(details)), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ctl_checkpoints(
 unit_id INTEGER PRIMARY KEY REFERENCES ctl_jobs(unit_id), result_digest TEXT NOT NULL,
 run_id TEXT REFERENCES ana_runs(id), artifacts TEXT NOT NULL CHECK(json_valid(artifacts)),
 handoff TEXT NOT NULL CHECK(json_valid(handoff)), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ctl_heads(
 candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES ana_runs(id),
 revision TEXT NOT NULL, stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0,1))
);
CREATE TABLE ctl_reviews(
 id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES ctl_heads(candidate_id),
 revision TEXT NOT NULL, verdict TEXT NOT NULL CHECK(verdict IN ('PASS','NEEDS_EVIDENCE','CONTRADICTED','PENDING','HELD')),
 stale INTEGER NOT NULL DEFAULT 0 CHECK(stale IN (0,1)), record TEXT NOT NULL CHECK(json_valid(record)),
 unit_id INTEGER NOT NULL REFERENCES ctl_jobs(unit_id)
);
CREATE TABLE ctl_publications(
 id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES ctl_heads(candidate_id), revision TEXT NOT NULL,
 review_id TEXT NOT NULL REFERENCES ctl_reviews(id), path TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL,
 valid INTEGER NOT NULL CHECK(valid IN (0,1)), template_sha256 TEXT NOT NULL,
 unit_id INTEGER NOT NULL REFERENCES ctl_jobs(unit_id), record TEXT NOT NULL CHECK(json_valid(record))
);
CREATE UNIQUE INDEX ctl_one_current_publication ON ctl_publications(candidate_id) WHERE valid=1;
CREATE TABLE ctl_changes(
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, reason TEXT NOT NULL, record TEXT NOT NULL CHECK(json_valid(record))
);
CREATE TABLE ctl_revalidation(
 change_id TEXT NOT NULL REFERENCES ctl_changes(id), candidate_id TEXT NOT NULL REFERENCES ctl_heads(candidate_id),
 revision TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('pending_approval','queued','resolved')),
 unit_id INTEGER REFERENCES ctl_jobs(unit_id), PRIMARY KEY(change_id,candidate_id)
);
CREATE TABLE ctl_splits(
 group_id TEXT PRIMARY KEY, manifest TEXT NOT NULL CHECK(json_valid(manifest)), merged_by INTEGER REFERENCES ctl_jobs(unit_id)
);
-- Track source dependencies through the *current* C02 relation graph.
CREATE VIEW ctl_source_dependencies AS
 WITH RECURSIVE dependencies(source_id,kind,object_id,revision) AS (
   SELECT l.source_version_id,'evidence',r.object_id,r.revision
   FROM ana_locations l JOIN ana_run_revisions r
     ON r.run_id=l.run_id AND r.kind='evidence' AND r.object_id=l.evidence_id
   WHERE l.source_version_id IS NOT NULL
   UNION
   SELECT d.source_id,e.from_kind,e.from_id,e.from_revision
   FROM dependencies d JOIN ana_edges e
     ON e.to_kind=d.kind AND e.to_id=d.object_id AND e.to_revision=d.revision
   WHERE e.run_id IN (SELECT run_id FROM ctl_heads)
 ) SELECT DISTINCT d.source_id,h.candidate_id FROM dependencies d JOIN ctl_heads h
   ON d.kind='candidate' AND d.object_id=h.candidate_id;
CREATE TRIGGER ctl_source_version AFTER INSERT ON source_versions
 WHEN EXISTS(SELECT 1 FROM source_versions old JOIN ctl_source_dependencies d ON d.source_id=old.id
   WHERE old.origin=NEW.origin AND old.source_key=NEW.source_key AND old.sha256<>NEW.sha256)
 BEGIN
 INSERT INTO ctl_changes(id,kind,reason,record) VALUES(
   'source-version:'||NEW.id,'source_version','new registered source version',json_object('source_version_id',NEW.id));
 INSERT INTO ctl_revalidation(change_id,candidate_id,revision,state)
   SELECT DISTINCT 'source-version:'||NEW.id,h.candidate_id,h.revision,'pending_approval'
   FROM source_versions old JOIN ctl_source_dependencies d ON d.source_id=old.id
   JOIN ctl_heads h ON h.candidate_id=d.candidate_id
   WHERE old.origin=NEW.origin AND old.source_key=NEW.source_key AND old.sha256<>NEW.sha256;
 UPDATE ctl_heads SET stale=1 WHERE candidate_id IN
   (SELECT candidate_id FROM ctl_revalidation WHERE change_id='source-version:'||NEW.id);
 UPDATE ctl_reviews SET stale=1 WHERE candidate_id IN
   (SELECT candidate_id FROM ctl_revalidation WHERE change_id='source-version:'||NEW.id);
 UPDATE ctl_publications SET valid=0 WHERE candidate_id IN
   (SELECT candidate_id FROM ctl_revalidation WHERE change_id='source-version:'||NEW.id);
 END;
