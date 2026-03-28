import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";

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

function keyFor(issuer: string, taskIdOrNonce: string): string {
  return `${issuer}:${taskIdOrNonce}`;
}

export class FileTaskStore {
  constructor(private readonly filePath: string) {}

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

  async reserveNonce(issuer: string, nonce: string, payloadSha256: string): Promise<"new" | "duplicate" | "conflict"> {
    const state = await this.readState();
    const key = keyFor(issuer, nonce);
    const existing = state.nonces[key];
    if (existing) {
      return existing.payload_sha256 === payloadSha256 ? "duplicate" : "conflict";
    }
    state.nonces[key] = { payload_sha256: payloadSha256 };
    await this.writeState(state);
    return "new";
  }

  async createTask(issuer: string, taskId: string, payloadSha256: string, taskPayload: Record<string, unknown>): Promise<TaskInsertResult> {
    const state = await this.readState();
    const key = keyFor(issuer, taskId);
    const existing = state.tasks[key];
    if (existing) {
      if (existing.payload_sha256 !== payloadSha256) {
        return { state: "conflict" };
      }
      return {
        state: "duplicate",
        task: existing,
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
      callback_url: callback?.url ?? null,
      callback_timeout_ms: callback?.timeout_ms ?? null,
      callback_state: callback ? "pending" : null
    };
    await this.writeState(state);
    return { state: "created" };
  }

  async getTask(issuer: string, taskId: string): Promise<Record<string, unknown> | null> {
    const state = await this.readState();
    return (state.tasks[keyFor(issuer, taskId)] as Record<string, unknown>) ?? null;
  }

  async saveResult(issuer: string, taskId: string, result: Record<string, unknown>): Promise<void> {
    const state = await this.readState();
    const key = keyFor(issuer, taskId);
    state.results[key] = result;
    if (state.tasks[key]) {
      state.tasks[key].status = result.status as string;
      state.tasks[key].completed_at = new Date().toISOString();
    }
    await this.writeState(state);
  }

  async getResult(issuer: string, taskId: string): Promise<Record<string, unknown> | null> {
    const state = await this.readState();
    return (state.results[keyFor(issuer, taskId)] as Record<string, unknown>) ?? null;
  }

  async statusSnapshot(issuer: string, taskId: string): Promise<{ task: Record<string, unknown> | null; result: Record<string, unknown> | null }> {
    const state = await this.readState();
    const key = keyFor(issuer, taskId);
    return {
      task: (state.tasks[key] as Record<string, unknown>) ?? null,
      result: (state.results[key] as Record<string, unknown>) ?? null
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
    state.next_callback_id += 1;
    await this.writeState(state);
  }

  async dueCallbacks(limit = 10): Promise<CallbackAttempt[]> {
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
  }

  async markCallbackDelivered(callbackId: number): Promise<void> {
    const state = await this.readState();
    const attempt = state.callback_attempts.find((candidate) => candidate.id === callbackId);
    if (attempt) {
      attempt.delivered_at = new Date().toISOString();
      await this.writeState(state);
    }
  }

  async rescheduleCallback(callbackId: number, delaySeconds: number, error: string): Promise<void> {
    const state = await this.readState();
    const attempt = state.callback_attempts.find((candidate) => candidate.id === callbackId);
    if (attempt) {
      attempt.scheduled_at = new Date(Date.now() + delaySeconds * 1000).toISOString();
      attempt.last_error = error;
      attempt.attempt += 1;
      await this.writeState(state);
    }
  }
}

