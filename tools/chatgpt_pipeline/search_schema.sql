CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations(
 id TEXT PRIMARY KEY,provider TEXT NOT NULL,account TEXT NOT NULL,conversation_id TEXT NOT NULL,
 revision TEXT NOT NULL,category TEXT NOT NULL CHECK(category IN ('DEV','AMBIGUOUS')),
 created_at TEXT,updated_at TEXT,current_node TEXT,body_status TEXT NOT NULL,
 UNIQUE(provider,account,conversation_id,revision)
);
CREATE TABLE IF NOT EXISTS conversation_locations(
 classification_id TEXT PRIMARY KEY,conversation TEXT NOT NULL REFERENCES conversations(id),
 batch TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,pointer TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nodes(
 conversation TEXT NOT NULL REFERENCES conversations(id),node_id TEXT NOT NULL,parent TEXT,
 declared_children TEXT NOT NULL,derived_children TEXT NOT NULL,message_key TEXT,
 PRIMARY KEY(conversation,node_id)
);
CREATE TABLE IF NOT EXISTS messages(
 id TEXT PRIMARY KEY,conversation TEXT NOT NULL REFERENCES conversations(id),node_id TEXT NOT NULL,
 provider TEXT NOT NULL,account TEXT NOT NULL,conversation_id TEXT NOT NULL,message_id TEXT NOT NULL,
 revision TEXT NOT NULL,id_kind TEXT NOT NULL,role TEXT NOT NULL,created_at TEXT,
 content_sha256 TEXT NOT NULL,pointer_suffix TEXT NOT NULL,semantic_kind TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('indexed','held','empty','excluded_role')),
 UNIQUE(conversation,node_id)
);
CREATE TABLE IF NOT EXISTS assets(
 id TEXT PRIMARY KEY,message TEXT NOT NULL REFERENCES messages(id),asset_id TEXT NOT NULL,
 pointer TEXT NOT NULL,kind TEXT NOT NULL,body_opened INTEGER NOT NULL DEFAULT 0 CHECK(body_opened=0)
);
CREATE TABLE IF NOT EXISTS segments(
 rowid INTEGER PRIMARY KEY,id TEXT NOT NULL UNIQUE,message TEXT NOT NULL REFERENCES messages(id),
 part_pointer TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,
 text TEXT NOT NULL,text_sha256 TEXT NOT NULL,rule TEXT NOT NULL,reason TEXT NOT NULL,redacted INTEGER NOT NULL,
 semantic_kind TEXT NOT NULL,analysis_unit TEXT NOT NULL,project_hints TEXT NOT NULL,
 CHECK(end>start), UNIQUE(message,part_pointer,start,end,rule)
);
CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(text, identifiers, content='',tokenize='unicode61');
-- Separate trigram index for Korean substrings, paths and error fragments (>=3 chars).
CREATE VIRTUAL TABLE IF NOT EXISTS substring_fts USING fts5(text,content='',tokenize='trigram');
CREATE TABLE IF NOT EXISTS relations(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,left_id TEXT NOT NULL,right_id TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('candidate','confirmed','insufficient','conflicting','not_found','not_searched')),
 reason TEXT NOT NULL,UNIQUE(kind,left_id,right_id)
);
CREATE TABLE IF NOT EXISTS github_docs(
 id TEXT PRIMARY KEY,source_version_id INTEGER NOT NULL,origin TEXT NOT NULL,project TEXT NOT NULL,
 pr_number INTEGER,kind TEXT NOT NULL,source_key TEXT NOT NULL,path TEXT NOT NULL,sha256 TEXT NOT NULL,
 pointer TEXT NOT NULL,object_id TEXT,author TEXT,created_at TEXT,text TEXT NOT NULL,
 UNIQUE(source_version_id,pointer)
);
CREATE VIRTUAL TABLE IF NOT EXISTS github_fts USING fts5(text,identifiers,content='',tokenize='unicode61');
CREATE VIRTUAL TABLE IF NOT EXISTS github_substrings USING fts5(text,content='',tokenize='trigram');
CREATE TABLE IF NOT EXISTS search_runs(
 id TEXT PRIMARY KEY,direction TEXT NOT NULL CHECK(direction IN ('github_to_chatgpt','chatgpt_to_github')),
 source_id TEXT NOT NULL,clues TEXT NOT NULL,scope TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('candidate','confirmed','insufficient','conflicting','not_found','not_searched')),
 reason TEXT NOT NULL,rule TEXT NOT NULL,hit_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS links(
 id TEXT PRIMARY KEY,search_run TEXT NOT NULL REFERENCES search_runs(id),segment TEXT NOT NULL REFERENCES segments(id),
 github_doc TEXT NOT NULL REFERENCES github_docs(id),direction TEXT NOT NULL,
 clues TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('candidate','confirmed','insufficient','conflicting')),
 reason TEXT NOT NULL,verification_status TEXT NOT NULL CHECK(verification_status IN ('pending','reviewed')),
 UNIQUE(search_run,segment,github_doc)
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages(conversation);
CREATE INDEX IF NOT EXISTS segments_message ON segments(message);
CREATE INDEX IF NOT EXISTS github_origin_pr ON github_docs(origin,pr_number);
CREATE VIEW IF NOT EXISTS searchable_segments AS
 SELECT s.*,m.account,m.conversation_id,m.message_id,m.revision,m.role,m.created_at,m.conversation,
 m.pointer_suffix,m.content_sha256 FROM segments s JOIN messages m ON s.message=m.id;
