import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { Pool, type PoolClient } from "pg";

export interface TaskInsertResult {
  state: "created" | "duplicate" | "conflict";
  task?: Record<string, unknown>;
  result?: Record<string, unknown>;
}

export interface CallbackAttempt {
  callback_id: number;
  issuer: string;
  task_id: string;
  url: string;
  timeout_ms: number;
  attempt: number;
}

export interface TaskStore {
  start(): Promise<void>;
  close(): Promise<void>;
  isReady(): Promise<boolean>;
  reserveNonce(issuer: string, nonce: string, payloadSha256: string): Promise<"new" | "duplicate" | "conflict">;
  createTask(
    issuer: string,
    taskId: string,
    payloadSha256: string,
    taskPayload: Record<string, unknown>
  ): Promise<TaskInsertResult>;
  getTask(issuer: string, taskId: string): Promise<Record<string, unknown> | null>;
  saveResult(issuer: string, taskId: string, result: Record<string, unknown>): Promise<void>;
  getResult(issuer: string, taskId: string): Promise<Record<string, unknown> | null>;
  statusSnapshot(issuer: string, taskId: string): Promise<{ task: Record<string, unknown> | null; result: Record<string, unknown> | null }>;
  enqueueCallback(
    issuer: string,
    taskId: string,
    url: string,
    timeoutMs: number,
    delaySeconds: number,
    attempt: number
  ): Promise<void>;
  dueCallbacks(limit?: number): Promise<CallbackAttempt[]>;
  markCallbackDelivered(callbackId: number): Promise<void>;
  rescheduleCallback(callbackId: number, delaySeconds: number, error: string): Promise<void>;
  failCallback(callbackId: number, error: string): Promise<void>;
}

interface StoreState {
  tasks: Record<string, Record<string, unknown>>;
  results: Record<string, Record<string, unknown>>;
  nonces: Record<string, { payload_sha256: string }>;
  callback_attempts: Array<{
    id: number;
    issuer: string;
    task_id: string;
    callback_url: string;
    callback_timeout_ms: number;
    attempt: number;
    scheduled_at: string;
    delivered_at: string | null;
    last_error: string | null;
  }>;
  next_callback_id: number;
}

const MIGRATION_LOCK_ID = 870421;
const DEFAULT_MIGRATIONS_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../sql/migrations");

function keyFor(issuer: string, taskIdOrNonce: string): string {
  return `${issuer}:${taskIdOrNonce}`;
}

function buildTaskRecord(
  payloadJson: Record<string, unknown>,
  storeValues: {
    status: string;
    accepted_at: string;
    completed_at: string | null;
    callback_url: string | null;
    callback_timeout_ms: number | null;
    callback_state: string | null;
  }
): Record<string, unknown> {
  return {
    ...structuredClone(payloadJson),
    _store: {
      status: storeValues.status,
      accepted_at: storeValues.accepted_at,
      completed_at: storeValues.completed_at,
      callback_url: storeValues.callback_url,
      callback_timeout_ms: storeValues.callback_timeout_ms,
      callback_state: storeValues.callback_state
    }
  };
}

export class FileTaskStore implements TaskStore {
  private ready = false;
  private lock: Promise<void> = Promise.resolve();

  constructor(private readonly filePath: string) {}

  async start(): Promise<void> {
    await this.ensureFile();
    this.ready = true;
  }

  async close(): Promise<void> {
    this.ready = false;
  }

  async isReady(): Promise<boolean> {
    return this.ready;
  }

  private async ensureFile(): Promise<void> {
    await mkdir(path.dirname(this.filePath), { recursive: true });
    try {
      await readFile(this.filePath, "utf8");
    } catch {
      await this.writeState({
        tasks: {},
        results: {},
        nonces: {},
        callback_attempts: [],
        next_callback_id: 1
      });
    }
  }

  private async readState(): Promise<StoreState> {
    await this.ensureFile();
    return JSON.parse(await readFile(this.filePath, "utf8")) as StoreState;
  }

  private async writeState(state: StoreState): Promise<void> {
    await writeFile(this.filePath, JSON.stringify(state, null, 2));
  }

  private async withLock<T>(operation: () => Promise<T>): Promise<T> {
    const previous = this.lock;
    let release!: () => void;
    this.lock = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous;
    try {
      return await operation();
    } finally {
      release();
    }
  }

  async reserveNonce(issuer: string, nonce: string, payloadSha256: string): Promise<"new" | "duplicate" | "conflict"> {
    return this.withLock(async () => {
      const state = await this.readState();
      const key = keyFor(issuer, nonce);
      const existing = state.nonces[key];
      if (existing) {
        return existing.payload_sha256 === payloadSha256 ? "duplicate" : "conflict";
      }
      state.nonces[key] = { payload_sha256: payloadSha256 };
      await this.writeState(state);
      return "new";
    });
  }

  async createTask(issuer: string, taskId: string, payloadSha256: string, taskPayload: Record<string, unknown>): Promise<TaskInsertResult> {
    return this.withLock(async () => {
      const state = await this.readState();
      const key = keyFor(issuer, taskId);
      const existing = state.tasks[key];
      if (existing) {
        if (existing.payload_sha256 !== payloadSha256) {
          return { state: "conflict" };
        }
        const payload = structuredClone((existing.payload_json as Record<string, unknown>) ?? {});
        return {
          state: "duplicate",
          task: buildTaskRecord(payload, {
            status: String(existing.status ?? "accepted"),
            accepted_at: String(existing.accepted_at ?? new Date().toISOString()),
            completed_at: (existing.completed_at as string | null | undefined) ?? null,
            callback_url: (existing.callback_url as string | null | undefined) ?? null,
            callback_timeout_ms: (existing.callback_timeout_ms as number | null | undefined) ?? null,
            callback_state: (existing.callback_state as string | null | undefined) ?? null
          }),
          result: state.results[key]
        };
      }
      const responseMode = taskPayload.response_mode as Record<string, unknown>;
      const callback = responseMode.callback as Record<string, unknown> | undefined;
      state.tasks[key] = {
        payload_sha256: payloadSha256,
        payload_json: taskPayload,
        status: "accepted",
        accepted_at: new Date().toISOString(),
        completed_at: null,
        callback_url: callback?.url ?? null,
        callback_timeout_ms: callback?.timeout_ms ?? null,
        callback_state: callback ? "pending" : null
      };
      await this.writeState(state);
      return { state: "created" };
    });
  }

  async getTask(issuer: string, taskId: string): Promise<Record<string, unknown> | null> {
    return this.withLock(async () => {
      const state = await this.readState();
      const existing = state.tasks[keyFor(issuer, taskId)];
      if (!existing) {
        return null;
      }
      return buildTaskRecord(structuredClone(existing.payload_json as Record<string, unknown>), {
        status: String(existing.status ?? "accepted"),
        accepted_at: String(existing.accepted_at ?? new Date().toISOString()),
        completed_at: (existing.completed_at as string | null | undefined) ?? null,
        callback_url: (existing.callback_url as string | null | undefined) ?? null,
        callback_timeout_ms: (existing.callback_timeout_ms as number | null | undefined) ?? null,
        callback_state: (existing.callback_state as string | null | undefined) ?? null
      });
    });
  }

  async saveResult(issuer: string, taskId: string, result: Record<string, unknown>): Promise<void> {
    await this.withLock(async () => {
      const state = await this.readState();
      const key = keyFor(issuer, taskId);
      state.results[key] = result;
      if (state.tasks[key]) {
        state.tasks[key].status = String(result.status ?? "success");
        state.tasks[key].completed_at = new Date().toISOString();
      }
      await this.writeState(state);
    });
  }

  async getResult(issuer: string, taskId: string): Promise<Record<string, unknown> | null> {
    return this.withLock(async () => {
      const state = await this.readState();
      return (state.results[keyFor(issuer, taskId)] as Record<string, unknown>) ?? null;
    });
  }

  async statusSnapshot(issuer: string, taskId: string): Promise<{ task: Record<string, unknown> | null; result: Record<string, unknown> | null }> {
    return {
      task: await this.getTask(issuer, taskId),
      result: await this.getResult(issuer, taskId)
    };
  }

  async enqueueCallback(
    issuer: string,
    taskId: string,
    url: string,
    timeoutMs: number,
    delaySeconds: number,
    attempt: number
  ): Promise<void> {
    await this.withLock(async () => {
      const state = await this.readState();
      state.callback_attempts.push({
        id: state.next_callback_id,
        issuer,
        task_id: taskId,
        callback_url: url,
        callback_timeout_ms: timeoutMs,
        attempt,
        scheduled_at: new Date(Date.now() + delaySeconds * 1000).toISOString(),
        delivered_at: null,
        last_error: null
      });
      const key = keyFor(issuer, taskId);
      if (state.tasks[key]) {
        state.tasks[key].callback_state = "pending";
      }
      state.next_callback_id += 1;
      await this.writeState(state);
    });
  }

  async dueCallbacks(limit = 10): Promise<CallbackAttempt[]> {
    return this.withLock(async () => {
      const state = await this.readState();
      const now = Date.now();
      return state.callback_attempts
        .filter((attempt) => attempt.delivered_at === null && new Date(attempt.scheduled_at).getTime() <= now)
        .slice(0, limit)
        .map((attempt) => ({
          callback_id: attempt.id,
          issuer: attempt.issuer,
          task_id: attempt.task_id,
          url: attempt.callback_url,
          timeout_ms: attempt.callback_timeout_ms,
          attempt: attempt.attempt
        }));
    });
  }

  async markCallbackDelivered(callbackId: number): Promise<void> {
    await this.withLock(async () => {
      const state = await this.readState();
      const attempt = state.callback_attempts.find((candidate) => candidate.id === callbackId);
      if (attempt) {
        attempt.delivered_at = new Date().toISOString();
        const key = keyFor(attempt.issuer, attempt.task_id);
        if (state.tasks[key]) {
          state.tasks[key].callback_state = "delivered";
        }
        await this.writeState(state);
      }
    });
  }

  async rescheduleCallback(callbackId: number, delaySeconds: number, error: string): Promise<void> {
    await this.withLock(async () => {
      const state = await this.readState();
      const attempt = state.callback_attempts.find((candidate) => candidate.id === callbackId);
      if (attempt) {
        attempt.scheduled_at = new Date(Date.now() + delaySeconds * 1000).toISOString();
        attempt.last_error = error;
        attempt.attempt += 1;
        await this.writeState(state);
      }
    });
  }

  async failCallback(callbackId: number, error: string): Promise<void> {
    await this.withLock(async () => {
      const state = await this.readState();
      const attempt = state.callback_attempts.find((candidate) => candidate.id === callbackId);
      if (attempt) {
        attempt.delivered_at = new Date().toISOString();
        attempt.last_error = error;
        const key = keyFor(attempt.issuer, attempt.task_id);
        if (state.tasks[key]) {
          state.tasks[key].callback_state = "failed";
        }
        await this.writeState(state);
      }
    });
  }
}

export class PostgresTaskStore implements TaskStore {
  private pool: Pool | null = null;
  private ready = false;

  constructor(
    private readonly databaseUrl: string,
    private readonly migrationsDir = DEFAULT_MIGRATIONS_DIR
  ) {}

  async start(): Promise<void> {
    this.pool = new Pool({ connectionString: this.databaseUrl, max: 10 });
    await this.applyMigrations();
    this.ready = true;
  }

  async close(): Promise<void> {
    if (this.pool) {
      await this.pool.end();
      this.pool = null;
    }
    this.ready = false;
  }

  async isReady(): Promise<boolean> {
    if (!this.ready || !this.pool) {
      return false;
    }
    try {
      await this.pool.query("SELECT 1");
      return true;
    } catch {
      return false;
    }
  }

  async reserveNonce(issuer: string, nonce: string, payloadSha256: string): Promise<"new" | "duplicate" | "conflict"> {
    const client = await this.client();
    try {
      const existing = await client.query<{ payload_sha256: string }>(
        "SELECT payload_sha256 FROM nonces WHERE issuer = $1 AND nonce = $2",
        [issuer, nonce]
      );
      if (existing.rowCount && existing.rows[0]) {
        return existing.rows[0].payload_sha256 === payloadSha256 ? "duplicate" : "conflict";
      }
      await client.query(
        "INSERT INTO nonces (issuer, nonce, payload_sha256, created_at) VALUES ($1, $2, $3, NOW())",
        [issuer, nonce, payloadSha256]
      );
      return "new";
    } finally {
      client.release();
    }
  }

  async createTask(issuer: string, taskId: string, payloadSha256: string, taskPayload: Record<string, unknown>): Promise<TaskInsertResult> {
    const client = await this.client();
    try {
      const existing = await client.query<{
        payload_sha256: string;
        payload_json: Record<string, unknown>;
        status: string;
        accepted_at: Date;
        completed_at: Date | null;
        callback_url: string | null;
        callback_timeout_ms: number | null;
        callback_state: string | null;
      }>(
        `
          SELECT payload_sha256, payload_json, status, accepted_at, completed_at, callback_url, callback_timeout_ms, callback_state
          FROM tasks
          WHERE issuer = $1 AND task_id = $2
        `,
        [issuer, taskId]
      );
      if (existing.rowCount && existing.rows[0]) {
        const row = existing.rows[0];
        if (row.payload_sha256 !== payloadSha256) {
          return { state: "conflict" };
        }
        const result = await client.query<{ result_json: Record<string, unknown> }>(
          "SELECT result_json FROM results WHERE issuer = $1 AND task_id = $2",
          [issuer, taskId]
        );
        return {
          state: "duplicate",
          task: buildTaskRecord(row.payload_json, {
            status: row.status,
            accepted_at: row.accepted_at.toISOString(),
            completed_at: row.completed_at ? row.completed_at.toISOString() : null,
            callback_url: row.callback_url,
            callback_timeout_ms: row.callback_timeout_ms,
            callback_state: row.callback_state
          }),
          result: result.rows[0]?.result_json
        };
      }

      const responseMode = taskPayload.response_mode as { mode: string; callback?: { url: string; timeout_ms: number } };
      const callbackUrl = responseMode.mode === "callback" ? responseMode.callback?.url ?? null : null;
      const callbackTimeoutMs = responseMode.mode === "callback" ? responseMode.callback?.timeout_ms ?? null : null;
      const callbackState = responseMode.mode === "callback" ? "pending" : null;

      await client.query(
        `
          INSERT INTO tasks (
            issuer, task_id, task_type, recipient, payload_sha256, payload_json, expected_output_schema,
            response_mode, status, accepted_at, callback_url, callback_timeout_ms, callback_state
          )
          VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8::jsonb, 'accepted', NOW(), $9, $10, $11)
        `,
        [
          issuer,
          taskId,
          String(taskPayload.task_type),
          String(taskPayload.recipient),
          payloadSha256,
          JSON.stringify(taskPayload),
          JSON.stringify(taskPayload.expected_output_schema),
          JSON.stringify(taskPayload.response_mode),
          callbackUrl,
          callbackTimeoutMs,
          callbackState
        ]
      );
      return { state: "created" };
    } finally {
      client.release();
    }
  }

  async getTask(issuer: string, taskId: string): Promise<Record<string, unknown> | null> {
    const client = await this.client();
    try {
      const result = await client.query<{
        payload_json: Record<string, unknown>;
        status: string;
        accepted_at: Date;
        completed_at: Date | null;
        callback_url: string | null;
        callback_timeout_ms: number | null;
        callback_state: string | null;
      }>(
        `
          SELECT payload_json, status, accepted_at, completed_at, callback_url, callback_timeout_ms, callback_state
          FROM tasks
          WHERE issuer = $1 AND task_id = $2
        `,
        [issuer, taskId]
      );
      const row = result.rows[0];
      if (!row) {
        return null;
      }
      return buildTaskRecord(row.payload_json, {
        status: row.status,
        accepted_at: row.accepted_at.toISOString(),
        completed_at: row.completed_at ? row.completed_at.toISOString() : null,
        callback_url: row.callback_url,
        callback_timeout_ms: row.callback_timeout_ms,
        callback_state: row.callback_state
      });
    } finally {
      client.release();
    }
  }

  async saveResult(issuer: string, taskId: string, result: Record<string, unknown>): Promise<void> {
    const client = await this.client();
    try {
      await client.query("BEGIN");
      await client.query(
        `
          INSERT INTO results (issuer, task_id, result_json, status, created_at)
          VALUES ($1, $2, $3::jsonb, $4, NOW())
          ON CONFLICT (issuer, task_id) DO UPDATE SET
            result_json = EXCLUDED.result_json,
            status = EXCLUDED.status,
            created_at = NOW()
        `,
        [issuer, taskId, JSON.stringify(result), String(result.status ?? "success")]
      );
      await client.query(
        "UPDATE tasks SET status = $1, completed_at = NOW() WHERE issuer = $2 AND task_id = $3",
        [String(result.status ?? "success"), issuer, taskId]
      );
      await client.query("COMMIT");
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }

  async getResult(issuer: string, taskId: string): Promise<Record<string, unknown> | null> {
    const client = await this.client();
    try {
      const result = await client.query<{ result_json: Record<string, unknown> }>(
        "SELECT result_json FROM results WHERE issuer = $1 AND task_id = $2",
        [issuer, taskId]
      );
      return result.rows[0]?.result_json ?? null;
    } finally {
      client.release();
    }
  }

  async statusSnapshot(issuer: string, taskId: string): Promise<{ task: Record<string, unknown> | null; result: Record<string, unknown> | null }> {
    return {
      task: await this.getTask(issuer, taskId),
      result: await this.getResult(issuer, taskId)
    };
  }

  async enqueueCallback(
    issuer: string,
    taskId: string,
    url: string,
    timeoutMs: number,
    delaySeconds: number,
    attempt: number
  ): Promise<void> {
    const client = await this.client();
    try {
      await client.query("BEGIN");
      await client.query(
        `
          INSERT INTO callback_attempts (
            issuer, task_id, callback_url, callback_timeout_ms, attempt, scheduled_at
          ) VALUES ($1, $2, $3, $4, $5, NOW() + ($6::text || ' seconds')::interval)
        `,
        [issuer, taskId, url, timeoutMs, attempt, delaySeconds]
      );
      await client.query("UPDATE tasks SET callback_state = 'pending' WHERE issuer = $1 AND task_id = $2", [issuer, taskId]);
      await client.query("COMMIT");
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }

  async dueCallbacks(limit = 10): Promise<CallbackAttempt[]> {
    const client = await this.client();
    try {
      const result = await client.query<{
        id: number;
        issuer: string;
        task_id: string;
        callback_url: string;
        callback_timeout_ms: number;
        attempt: number;
      }>(
        `
          SELECT id, issuer, task_id, callback_url, callback_timeout_ms, attempt
          FROM callback_attempts
          WHERE delivered_at IS NULL AND scheduled_at <= NOW()
          ORDER BY scheduled_at ASC
          LIMIT $1
        `,
        [limit]
      );
      return result.rows.map((row: { id: number; issuer: string; task_id: string; callback_url: string; callback_timeout_ms: number; attempt: number }) => ({
        callback_id: row.id,
        issuer: row.issuer,
        task_id: row.task_id,
        url: row.callback_url,
        timeout_ms: row.callback_timeout_ms,
        attempt: row.attempt
      }));
    } finally {
      client.release();
    }
  }

  async markCallbackDelivered(callbackId: number): Promise<void> {
    const client = await this.client();
    try {
      await client.query("BEGIN");
      const result = await client.query<{ issuer: string; task_id: string }>(
        "SELECT issuer, task_id FROM callback_attempts WHERE id = $1",
        [callbackId]
      );
      await client.query("UPDATE callback_attempts SET delivered_at = NOW() WHERE id = $1", [callbackId]);
      const row = result.rows[0];
      if (row) {
        await client.query("UPDATE tasks SET callback_state = 'delivered' WHERE issuer = $1 AND task_id = $2", [row.issuer, row.task_id]);
      }
      await client.query("COMMIT");
    } catch (error) {
      await client.query("ROLLBACK");
      throw error;
    } finally {
      client.release();
    }
  }

  async rescheduleCallback(callbackId: number, delaySeconds: number, error: string): Promise<void> {
    const client = await this.client();
    try {
      await client.query(
        `
          UPDATE callback_attempts
          SET scheduled_at = NOW() + ($1::text || ' seconds')::interval,
              last_error = $2,
              attempt = attempt + 1
          WHERE id = $3
        `,
        [delaySeconds, error, callbackId]
      );
    } finally {
      client.release();
    }
  }

  async failCallback(callbackId: number, error: string): Promise<void> {
    const client = await this.client();
    try {
      await client.query("BEGIN");
      const result = await client.query<{ issuer: string; task_id: string }>(
        "SELECT issuer, task_id FROM callback_attempts WHERE id = $1",
        [callbackId]
      );
      await client.query("UPDATE callback_attempts SET delivered_at = NOW(), last_error = $1 WHERE id = $2", [error, callbackId]);
      const row = result.rows[0];
      if (row) {
        await client.query("UPDATE tasks SET callback_state = 'failed' WHERE issuer = $1 AND task_id = $2", [row.issuer, row.task_id]);
      }
      await client.query("COMMIT");
    } catch (error_) {
      await client.query("ROLLBACK");
      throw error_;
    } finally {
      client.release();
    }
  }

  private async applyMigrations(): Promise<void> {
    const client = await this.client();
    try {
      await client.query("SELECT pg_advisory_lock($1)", [MIGRATION_LOCK_ID]);
      await client.query(
        `
          CREATE TABLE IF NOT EXISTS alp_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
          )
        `
      );
      const files = (await readMigrations(this.migrationsDir)).sort((a, b) => a.name.localeCompare(b.name));
      for (const file of files) {
        const exists = await client.query("SELECT 1 FROM alp_migrations WHERE version = $1", [file.name]);
        if (exists.rowCount) {
          continue;
        }
        await client.query(file.contents);
        await client.query("INSERT INTO alp_migrations (version) VALUES ($1)", [file.name]);
      }
    } finally {
      await client.query("SELECT pg_advisory_unlock($1)", [MIGRATION_LOCK_ID]);
      client.release();
    }
  }

  private async client(): Promise<PoolClient> {
    if (!this.pool) {
      throw new Error("PostgresTaskStore is not started");
    }
    return this.pool.connect();
  }
}

async function readMigrations(migrationsDir: string): Promise<Array<{ name: string; contents: string }>>;
async function readMigrations(migrationsDir: string | undefined): Promise<Array<{ name: string; contents: string }>>;
async function readMigrations(migrationsDir: string | undefined): Promise<Array<{ name: string; contents: string }>> {
  const dir = migrationsDir ?? DEFAULT_MIGRATIONS_DIR;
  await mkdir(dir, { recursive: true });
  const { readdir } = await import("node:fs/promises");
  const files = await readdir(dir);
  const sqlFiles = files.filter((file) => file.endsWith(".sql")).sort();
  const migrations: Array<{ name: string; contents: string }> = [];
  for (const file of sqlFiles) {
    migrations.push({
      name: file,
      contents: await readFile(path.join(dir, file), "utf8")
    });
  }
  return migrations;
}
