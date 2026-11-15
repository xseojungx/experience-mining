-- C02-1, additive only. No UPDATE/ALTER/DROP of existing collection tables.
CREATE TABLE ana_migrations (
 version INTEGER PRIMARY KEY CHECK(version=2),
 name TEXT NOT NULL UNIQUE, sha256 TEXT NOT NULL CHECK(length(sha256)=64),
 base_sha256 TEXT NOT NULL CHECK(length(base_sha256)=64),
 applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ana_artifacts (
 id INTEGER PRIMARY KEY,
 path TEXT NOT NULL CHECK(length(path)>0),
 sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
 version TEXT NOT NULL CHECK(length(version)>0),
 UNIQUE(path,sha256,version)
);
CREATE TABLE ana_runs (
 id TEXT PRIMARY KEY,
 input_digest TEXT NOT NULL UNIQUE CHECK(length(input_digest)=64),
 contract_version TEXT NOT NULL, spec_version TEXT NOT NULL, analysis_version TEXT NOT NULL,
 adapter_version TEXT NOT NULL,
 grade TEXT NOT NULL CHECK(grade IN ('c01_valid','legacy_incomplete')),
 result_artifact INTEGER NOT NULL REFERENCES ana_artifacts(id),
 diagnostics TEXT NOT NULL CHECK(json_valid(diagnostics)),
 bundle TEXT NOT NULL CHECK(json_valid(bundle)),
 completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE ana_inputs (
 run_id TEXT NOT NULL REFERENCES ana_runs(id),
 artifact_id INTEGER NOT NULL REFERENCES ana_artifacts(id),
 PRIMARY KEY(run_id,artifact_id)
);
-- Stable IDs and immutable content-addressed revisions for all six C01 types.
CREATE TABLE ana_objects (
 kind TEXT NOT NULL CHECK(kind IN ('candidate','claim','evidence','assessment','verification','note')),
 id TEXT NOT NULL CHECK(length(id)>0),
 PRIMARY KEY(kind,id)
);
CREATE TABLE ana_revisions (
 kind TEXT NOT NULL, object_id TEXT NOT NULL,
 revision TEXT NOT NULL CHECK(length(revision)=64 AND revision NOT GLOB '*[^0-9a-f]*'),
 logical_revision TEXT,
 previous_revision TEXT,
 payload TEXT NOT NULL CHECK(json_valid(payload) AND json_type(payload)='object'),
 PRIMARY KEY(kind,object_id,revision),
 FOREIGN KEY(kind,object_id) REFERENCES ana_objects(kind,id),
 FOREIGN KEY(kind,object_id,previous_revision) REFERENCES ana_revisions(kind,object_id,revision),
 CHECK(previous_revision IS NULL OR previous_revision<>revision),
 CHECK(json_extract(payload,'$.id') IS NOT NULL AND json_extract(payload,'$.id')=object_id),
 CHECK(kind<>'candidate' OR COALESCE(json_extract(payload,'$.state') IN
 ('DISCOVERED','INGESTED','MINED','LINKED','INVESTIGATED','VERIFIED','PUBLISHED'),0))
);
CREATE TABLE ana_run_revisions (
 run_id TEXT NOT NULL REFERENCES ana_runs(id),
 kind TEXT NOT NULL, object_id TEXT NOT NULL, revision TEXT NOT NULL,
 PRIMARY KEY(run_id,kind,object_id),
 FOREIGN KEY(kind,object_id,revision) REFERENCES ana_revisions(kind,object_id,revision)
);
CREATE TABLE ana_edges (
 run_id TEXT NOT NULL,
 from_kind TEXT NOT NULL, from_id TEXT NOT NULL, from_revision TEXT NOT NULL,
 relation TEXT NOT NULL CHECK(relation IN
 ('candidate_claim','candidate_assessment','candidate_verification','candidate_note',
 'supports','refutes','context','legacy_unclassified','score_claim','review_claim','review_evidence',
 'depends_on','affected_by')),
 to_kind TEXT NOT NULL, to_id TEXT NOT NULL, to_revision TEXT NOT NULL,
 PRIMARY KEY(run_id,from_kind,from_id,relation,to_kind,to_id),
 FOREIGN KEY(run_id,from_kind,from_id) REFERENCES ana_run_revisions(run_id,kind,object_id),
 FOREIGN KEY(run_id,to_kind,to_id) REFERENCES ana_run_revisions(run_id,kind,object_id),
 FOREIGN KEY(from_kind,from_id,from_revision) REFERENCES ana_revisions(kind,object_id,revision),
 FOREIGN KEY(to_kind,to_id,to_revision) REFERENCES ana_revisions(kind,object_id,revision),
 CHECK((relation='candidate_claim' AND from_kind='candidate' AND to_kind='claim') OR
 (relation='candidate_assessment' AND from_kind='candidate' AND to_kind='assessment') OR
 (relation='candidate_verification' AND from_kind='candidate' AND to_kind='verification') OR
 (relation='candidate_note' AND from_kind='candidate' AND to_kind='note') OR
 (relation IN ('supports','refutes','context','legacy_unclassified') AND from_kind='claim' AND to_kind='evidence') OR
 (relation='score_claim' AND from_kind='assessment' AND to_kind='claim') OR
 (relation='review_claim' AND from_kind='verification' AND to_kind='claim') OR
 (relation='review_evidence' AND from_kind='verification' AND to_kind='evidence') OR
 (relation IN ('depends_on','affected_by') AND NOT(from_kind=to_kind AND from_id=to_id)))
);
CREATE TRIGGER ana_edge_revision BEFORE INSERT ON ana_edges BEGIN
 SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM ana_run_revisions WHERE run_id=NEW.run_id
 AND kind=NEW.from_kind AND object_id=NEW.from_id AND revision=NEW.from_revision)
 OR NOT EXISTS(SELECT 1 FROM ana_run_revisions WHERE run_id=NEW.run_id
 AND kind=NEW.to_kind AND object_id=NEW.to_id AND revision=NEW.to_revision)
 THEN RAISE(ABORT,'edge revision outside run') END;
END;
-- Exact in-source selectors; unknown fields stay NULL in legacy rows.
CREATE TABLE ana_locations (
 run_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
 evidence_kind TEXT NOT NULL DEFAULT 'evidence' CHECK(evidence_kind='evidence'),
 raw_path TEXT, snapshot_sha256 TEXT CHECK(snapshot_sha256 IS NULL OR (length(snapshot_sha256)=64 AND snapshot_sha256 NOT GLOB '*[^0-9a-f]*')),
 source_version_id INTEGER REFERENCES source_versions(id),
 selector TEXT NOT NULL CHECK(json_valid(selector)),
 PRIMARY KEY(run_id,evidence_id),
 FOREIGN KEY(run_id,evidence_kind,evidence_id) REFERENCES ana_run_revisions(run_id,kind,object_id)
);
CREATE TABLE ana_issues (
 run_id TEXT NOT NULL, verification_id TEXT NOT NULL,
 verification_kind TEXT NOT NULL DEFAULT 'verification' CHECK(verification_kind='verification'),
 id TEXT NOT NULL, description TEXT NOT NULL,
 blocking INTEGER NOT NULL CHECK(blocking IN (0,1)),
 status TEXT NOT NULL CHECK(status IN ('open','resolved')),
 resolution TEXT, PRIMARY KEY(run_id,verification_id,id),
 FOREIGN KEY(run_id,verification_kind,verification_id) REFERENCES ana_run_revisions(run_id,kind,object_id),
 CHECK(status<>'resolved' OR (resolution IS NOT NULL AND length(trim(resolution))>0))
);
CREATE INDEX ana_edges_target ON ana_edges(to_kind,to_id,to_revision);
CREATE VIEW ana_candidate_revisions AS SELECT * FROM ana_revisions WHERE kind='candidate';
CREATE VIEW ana_claim_revisions AS SELECT * FROM ana_revisions WHERE kind='claim';
CREATE VIEW ana_evidence_revisions AS SELECT * FROM ana_revisions WHERE kind='evidence';
CREATE VIEW ana_assessment_versions AS SELECT * FROM ana_revisions WHERE kind='assessment';
CREATE VIEW ana_verification_runs AS SELECT * FROM ana_revisions WHERE kind='verification';
CREATE VIEW ana_note_revisions AS SELECT * FROM ana_revisions WHERE kind='note';
CREATE TRIGGER ana_revision_immutable BEFORE UPDATE ON ana_revisions BEGIN
 SELECT RAISE(ABORT,'append a revision instead'); END;
