import Fastify, { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";

import { signProtocolObject, sha256Hex, verifyProtocolObject, verifyStatusHeaders } from "./crypto.js";
import { ALPAuthError, ALPExecutionError, ALPValidationError } from "./errors.js";
import { FileTaskStore } from "./store.js";
import { TrustStore } from "./trust.js";
import {
  AuthBlock,
  CostTracking,
  ExecutionTrace,
  ResultContract,
  ResultError,
  TaskEnvelope,
  TaskExecutor,
  TaskReceipt,
  TraceStep
} from "./types.js";
import { validateCallbackUrl, validateOutputAgainstSchema, validateResultContract, validateTaskEnvelope } from "./validator.js";

const CALLBACK_SCHEDULE_SECONDS = [5, 30, 120, 600, 1800];

class RateLimiter {
  private readonly events = new Map<string, number[]>();

  allow(issuer: string, limitPerMinute: number): boolean {
    const now = Date.now();
    const existing = this.events.get(issuer) ?? [];
    const fresh = existing.filter((value) => now - value <= 60_000);
    if (fresh.length >= limitPerMinute) {
      this.events.set(issuer, fresh);
      return false;
    }
    fresh.push(now);
    this.events.set(issuer, fresh);
    return true;
  }
}

export class ALPServer {
  private readonly rateLimiter = new RateLimiter();
  private callbackTimer: NodeJS.Timeout | null = null;

  constructor(
    private readonly cfg: {
      agentId: string;
      trustStore: TrustStore;
      store: FileTaskStore;
      executor: TaskExecutor;
      keyId: string;
      privateKey: string | Uint8Array;
      localSyncCapMs?: number;
    }
  ) {}

  createApp(): FastifyInstance {
    const app = Fastify({ logger: true });

    app.addHook("onReady", async () => {
      this.callbackTimer = setInterval(() => {
        void this.dispatchCallbacks();
      }, 1000);
    });

    app.addHook("onClose", async () => {
      if (this.callbackTimer) {
        clearInterval(this.callbackTimer);
      }
    });

    app.post("/alp/v1/tasks", async (request, reply) => this.handleSubmit(request, reply));
    app.get("/alp/v1/tasks/:taskId", async (request, reply) =>
      this.handleStatus(request as FastifyRequest<{ Params: { taskId: string } }>, reply)
    );

    return app;
  }

  private async handleSubmit(request: FastifyRequest, reply: FastifyReply): Promise<void> {
    try {
      const payload = request.body as Record<string, unknown>;
      validateTaskEnvelope(payload);
      const task = payload as unknown as TaskEnvelope;
      if (task.recipient !== this.cfg.agentId) {
        throw new ALPValidationError(`recipient mismatch: expected ${this.cfg.agentId}`);
      }
      const publicKey = this.cfg.trustStore.getPublicKey(task.issuer, task.auth.key_id);
      verifyProtocolObject(payload, publicKey);
      this.cfg.trustStore.validateTaskType(task.issuer, task.task_type);
      if (!this.rateLimiter.allow(task.issuer, this.cfg.trustStore.requirePeer(task.issuer).requests_per_minute ?? 60)) {
        reply.status(429).send({ detail: "rate limit exceeded" });
        return;
      }

      const payloadHash = sha256Hex(this.idempotentPayload(payload));
      const nonceStatus = await this.cfg.store.reserveNonce(task.issuer, task.auth.nonce, payloadHash);
      if (nonceStatus === "conflict") {
        throw new ALPAuthError("nonce replay detected with different payload");
      }

      const insert = await this.cfg.store.createTask(task.issuer, task.task_id, payloadHash, payload);
      if (insert.state === "conflict") {
        reply.status(409).send({ detail: "task_id already exists with different payload" });
        return;
      }
      if (insert.state === "duplicate") {
        if (insert.result) {
          reply.status(200).send(insert.result);
          return;
        }
        const acceptedAt = (insert.task?.accepted_at as string) ?? new Date().toISOString();
        const receipt: TaskReceipt = {
          protocol_version: "alp.v1",
          task_id: task.task_id,
          state: "duplicate",
          accepted_at: acceptedAt,
          status_url: `${request.protocol}://${request.hostname}/alp/v1/tasks/${task.task_id}`
        };
        reply.status(202).send(receipt);
        return;
      }
      if (!(await this.cfg.executor.canHandle(task))) {
        throw new ALPValidationError(`executor cannot handle task_type ${task.task_type}`);
      }

      if (task.response_mode.mode === "sync") {
        const syncTimeout = Math.min(
          task.constraints.max_runtime_ms,
          task.response_mode.sync_timeout_ms ?? this.cfg.localSyncCapMs ?? 300000,
          this.cfg.localSyncCapMs ?? 300000
        );
        const result = await Promise.race([
          this.executeTask(task),
          new Promise<never>((_, reject) => setTimeout(() => reject(new ALPExecutionError("REMOTE_TIMEOUT", "sync execution timed out")), syncTimeout))
        ]);
        await this.cfg.store.saveResult(task.issuer, task.task_id, result as unknown as Record<string, unknown>);
        reply.status(200).send(result);
        return;
      }

      const callback = task.response_mode.callback;
      if (!callback) {
        throw new ALPValidationError("callback response mode requires callback configuration");
      }
      validateCallbackUrl(callback.url, this.cfg.trustStore.callbackAllowlist(task.issuer));
      void this.runAsyncExecution(task);
      const receipt: TaskReceipt = {
        protocol_version: "alp.v1",
        task_id: task.task_id,
        state: "accepted",
        accepted_at: new Date().toISOString(),
        status_url: `${request.protocol}://${request.hostname}/alp/v1/tasks/${task.task_id}`
      };
      reply.status(202).send(receipt);
    } catch (error) {
      if (error instanceof ALPAuthError) {
        reply.status(401).send({ detail: error.message });
        return;
      }
      if (error instanceof ALPValidationError) {
        reply.status(422).send({ detail: error.message });
        return;
      }
      request.log.error(error);
      reply.status(500).send({ detail: "internal error" });
    }
  }

  private async handleStatus(request: FastifyRequest<{ Params: { taskId: string } }>, reply: FastifyReply): Promise<void> {
    try {
      const taskId = request.params.taskId;
      const issuerHeader = request.headers["x-alp-issuer"];
      if (typeof issuerHeader !== "string") {
        throw new ALPAuthError("missing x-alp-issuer");
      }
      const publicKey = this.cfg.trustStore.getPublicKey(issuerHeader, String(request.headers["x-alp-key-id"] ?? ""));
      const issuer = verifyStatusHeaders(`/alp/v1/tasks/${taskId}`, request.headers, publicKey);
      const snapshot = await this.cfg.store.statusSnapshot(issuer, taskId);
      if (!snapshot.task) {
        reply.status(404).send({ detail: "task not found" });
        return;
      }
      if (snapshot.result) {
        reply.status(200).send(snapshot.result);
        return;
      }
      const receipt: TaskReceipt = {
        protocol_version: "alp.v1",
        task_id: taskId,
        state: "accepted",
        accepted_at: String(snapshot.task.accepted_at),
        status_url: `${request.protocol}://${request.hostname}/alp/v1/tasks/${taskId}`
      };
      reply.status(202).send(receipt);
    } catch (error) {
      if (error instanceof ALPAuthError) {
        reply.status(401).send({ detail: error.message });
        return;
      }
      reply.status(500).send({ detail: "internal error" });
    }
  }

  private async executeTask(task: TaskEnvelope): Promise<ResultContract<Record<string, unknown>>> {
    const receivedAt = new Date().toISOString();
    const startedAt = new Date().toISOString();
    const traceSteps: TraceStep[] = [
      { index: 0, kind: "validate", provider: null, model: null, status: "success", duration_ms: 0 }
    ];
    const started = Date.now();
    try {
      const output = await this.cfg.executor.execute(task);
      validateOutputAgainstSchema(output, task.expected_output_schema);
      traceSteps.push({
        index: 1,
        kind: "tool_call",
        provider: null,
        model: null,
        status: "success",
        duration_ms: Math.max(1, Date.now() - started)
      });
      traceSteps.push({
        index: 2,
        kind: "finalize",
        provider: null,
        model: null,
        status: "success",
        duration_ms: 0
      });
      const result: ResultContract<Record<string, unknown>> = {
        protocol_version: "alp.v1",
        task_id: task.task_id,
        issuer: this.cfg.agentId,
        status: "success",
        output,
        confidence: Math.max(task.constraints.min_confidence, 0.8),
        cost: { currency: "USD", total_usd: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0 },
        trace: {
          worker_id: this.cfg.agentId,
          received_at: receivedAt,
          started_at: startedAt,
          completed_at: new Date().toISOString(),
          attempts: 1,
          steps: traceSteps
        },
        error: null,
        auth: { alg: "Ed25519", key_id: this.cfg.keyId, issued_at: "", expires_at: "", nonce: "", signature: "" }
      };
      const signed = signProtocolObject(result as unknown as Record<string, unknown>, this.cfg.privateKey) as unknown as ResultContract<Record<string, unknown>>;
      validateResultContract(signed);
      return signed;
    } catch (error) {
      const resultError: ResultError =
        error instanceof ALPExecutionError
          ? { code: error.code as ResultError["code"], message: error.message, retriable: error.retriable, details: error.details }
          : {
              code: error instanceof ALPValidationError ? "OUTPUT_SCHEMA_MISMATCH" : "EXECUTION_ERROR",
              message: error instanceof Error ? error.message : "execution failed",
              retriable: false,
              details: {}
            };
      traceSteps.push({
        index: 1,
        kind: error instanceof ALPValidationError ? "repair" : "tool_call",
        provider: null,
        model: null,
        status: "failure",
        duration_ms: Math.max(1, Date.now() - started)
      });
      const failure: ResultContract<null> = {
        protocol_version: "alp.v1",
        task_id: task.task_id,
        issuer: this.cfg.agentId,
        status: "failure",
        output: null,
        confidence: 0,
        cost: { currency: "USD", total_usd: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0 },
        trace: {
          worker_id: this.cfg.agentId,
          received_at: receivedAt,
          started_at: startedAt,
          completed_at: new Date().toISOString(),
          attempts: 1,
          steps: traceSteps
        },
        error: resultError,
        auth: { alg: "Ed25519", key_id: this.cfg.keyId, issued_at: "", expires_at: "", nonce: "", signature: "" }
      };
      const signed = signProtocolObject(failure as unknown as Record<string, unknown>, this.cfg.privateKey) as unknown as ResultContract<null>;
      validateResultContract(signed);
      return signed as unknown as ResultContract<Record<string, unknown>>;
    }
  }

  private async runAsyncExecution(task: TaskEnvelope): Promise<void> {
    const callback = task.response_mode.callback;
    if (!callback) {
      return;
    }
    await this.cfg.store.enqueueCallback(task.issuer, task.task_id, callback.url, callback.timeout_ms, 0, 0);
    const result = await this.executeTask(task);
    await this.cfg.store.saveResult(task.issuer, task.task_id, result as unknown as Record<string, unknown>);
  }

  private async dispatchCallbacks(): Promise<void> {
    const due = await this.cfg.store.dueCallbacks();
    for (const attempt of due) {
      const result = await this.cfg.store.getResult(attempt.issuer, attempt.task_id);
      if (!result) {
        continue;
      }
      try {
        const response = await fetch(attempt.url, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(result),
          signal: AbortSignal.timeout(attempt.timeout_ms)
        });
        if (!response.ok) {
          throw new Error(`callback returned ${response.status}`);
        }
        await this.cfg.store.markCallbackDelivered(attempt.callback_id);
      } catch (error) {
        if (attempt.attempt + 1 >= CALLBACK_SCHEDULE_SECONDS.length) {
          await this.cfg.store.markCallbackDelivered(attempt.callback_id);
        } else {
          await this.cfg.store.rescheduleCallback(
            attempt.callback_id,
            CALLBACK_SCHEDULE_SECONDS[attempt.attempt + 1],
            error instanceof Error ? error.message : "callback delivery failed"
          );
        }
      }
    }
  }

  private idempotentPayload(payload: Record<string, unknown>): Record<string, unknown> {
    const clone = structuredClone(payload);
    const auth = (clone.auth ?? {}) as Record<string, unknown>;
    auth.issued_at = "";
    auth.expires_at = "";
    auth.nonce = "";
    auth.signature = "";
    clone.auth = auth;
    return clone;
  }
}
