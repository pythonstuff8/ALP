CREATE TABLE IF NOT EXISTS tasks (
  issuer TEXT NOT NULL,
  task_id TEXT NOT NULL,
  task_type TEXT NOT NULL,
  recipient TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  payload_json JSONB NOT NULL,
  expected_output_schema JSONB NOT NULL,
  response_mode JSONB NOT NULL,
  status TEXT NOT NULL,
  accepted_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  callback_url TEXT,
  callback_timeout_ms INTEGER,
  callback_state TEXT,
  PRIMARY KEY (issuer, task_id)
);

CREATE TABLE IF NOT EXISTS results (
  issuer TEXT NOT NULL,
  task_id TEXT NOT NULL,
  result_json JSONB NOT NULL,
  status TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (issuer, task_id),
  FOREIGN KEY (issuer, task_id) REFERENCES tasks (issuer, task_id)
);

CREATE TABLE IF NOT EXISTS nonces (
  issuer TEXT NOT NULL,
  nonce TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (issuer, nonce)
);

CREATE TABLE IF NOT EXISTS callback_attempts (
  id BIGSERIAL PRIMARY KEY,
  issuer TEXT NOT NULL,
  task_id TEXT NOT NULL,
  callback_url TEXT NOT NULL,
  callback_timeout_ms INTEGER NOT NULL,
  attempt INTEGER NOT NULL,
  scheduled_at TIMESTAMPTZ NOT NULL,
  delivered_at TIMESTAMPTZ,
  last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_callback_attempts_due
ON callback_attempts (scheduled_at)
WHERE delivered_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_status
ON tasks (status, accepted_at DESC);
