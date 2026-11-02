
CREATE TABLE IF NOT EXISTS schema_info(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_info VALUES(1);
CREATE TABLE IF NOT EXISTS knowledge_bases(
 kb_id TEXT PRIMARY KEY,name TEXT NOT NULL,model_json TEXT NOT NULL,model_revision TEXT NOT NULL,
 embedding_profile_json TEXT NOT NULL,maintenance_state TEXT NOT NULL DEFAULT 'READY',maintenance_job_id TEXT,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS documents(
 document_id TEXT PRIMARY KEY,kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),original_name TEXT NOT NULL,
 media_type TEXT NOT NULL,raw_path TEXT,byte_length INTEGER NOT NULL,source_json TEXT NOT NULL,mapping_id TEXT,
 active INTEGER NOT NULL DEFAULT 0,deleted_at TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 queue_seq INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT UNIQUE NOT NULL,kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
 operation TEXT NOT NULL,document_id TEXT REFERENCES documents(document_id),input_json TEXT NOT NULL,state TEXT NOT NULL,
 attempt INTEGER NOT NULL DEFAULT 0,plan_path TEXT,plan_id TEXT,last_step TEXT,error_json TEXT,counts_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL,next_attempt_at REAL NOT NULL DEFAULT 0,cleanup_pending INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS requests(
 kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),operation TEXT NOT NULL,idempotency_key TEXT NOT NULL,
 request_json TEXT NOT NULL,document_id TEXT,job_id TEXT,PRIMARY KEY(kb_id,operation,idempotency_key));
CREATE TABLE IF NOT EXISTS source_contributions(
 contribution_id TEXT PRIMARY KEY,kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),
 document_id TEXT NOT NULL REFERENCES documents(document_id),chunk_id TEXT NOT NULL,target_kind TEXT NOT NULL,
 target_id TEXT NOT NULL,field_path TEXT NOT NULL,claim_json TEXT NOT NULL,value_json TEXT NOT NULL,
 evidence_json TEXT NOT NULL,job_id TEXT NOT NULL REFERENCES jobs(job_id));
CREATE TABLE IF NOT EXISTS candidate_issues(
 issue_id TEXT PRIMARY KEY,kb_id TEXT NOT NULL REFERENCES knowledge_bases(kb_id),job_id TEXT NOT NULL REFERENCES jobs(job_id),
 document_id TEXT,chunk_id TEXT,candidate_json TEXT NOT NULL,reason_code TEXT NOT NULL,details_json TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(state,queue_seq);
CREATE INDEX IF NOT EXISTS documents_active ON documents(kb_id,active);
CREATE INDEX IF NOT EXISTS contributions_target ON source_contributions(kb_id,target_kind,target_id);
CREATE INDEX IF NOT EXISTS contributions_document ON source_contributions(kb_id,document_id);
CREATE INDEX IF NOT EXISTS issues_job ON candidate_issues(job_id);
