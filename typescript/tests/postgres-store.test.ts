import assert from "node:assert/strict";
import process from "node:process";
import test from "node:test";

import { PostgresTaskStore } from "../src/store.js";

const databaseUrl = process.env.TEST_DATABASE_URL;

test("postgres store roundtrip", { skip: !databaseUrl }, async () => {
  const issuer = `sender-${Date.now()}`;
  const taskId = `tsk_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
  const nonce = `nonce_${Math.random().toString(36).slice(2, 10)}`;
  const store = new PostgresTaskStore(databaseUrl!);

  const taskPayload = {
    protocol_version: "alp.v1",
    task_id: taskId,
    task_type: "com.acme.echo.v1",
    issuer,
    recipient: "receiver-b",
    created_at: "2026-03-28T14:00:00Z",
    objective: "Echo a string",
    inputs: { message: "hello" },
    constraints: {
      deadline_at: "2026-03-28T14:05:00Z",
      max_runtime_ms: 5000,
      max_cost_usd: 0.5,
      min_confidence: 0.5,
      quality_tier: "standard"
    },
    expected_output_schema: {
      schema_dialect: "alp.output-schema.v1",
      schema: {
        type: "object",
        properties: { echo: { type: "string" } },
        required: ["echo"],
        additionalProperties: false
      }
    },
    response_mode: { mode: "sync", sync_timeout_ms: 5000 },
    auth: {
      alg: "Ed25519",
      key_id: "sender-key",
      issued_at: "2026-03-28T14:00:00Z",
      expires_at: "2026-03-28T14:05:00Z",
      nonce,
      signature: "test-signature"
    }
  };

  const resultPayload = {
    protocol_version: "alp.v1",
    task_id: taskId,
    issuer: "receiver-b",
    status: "success",
    output: { echo: "hello" },
    confidence: 0.9,
    cost: { currency: "USD", total_usd: 0, input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    trace: {
      worker_id: "receiver-b",
      received_at: "2026-03-28T14:00:00Z",
      started_at: "2026-03-28T14:00:00Z",
      completed_at: "2026-03-28T14:00:01Z",
      attempts: 1,
      steps: [{ index: 0, kind: "validate", status: "success", duration_ms: 1 }]
    },
    auth: {
      alg: "Ed25519",
      key_id: "receiver-key",
      issued_at: "2026-03-28T14:00:00Z",
      expires_at: "2026-03-28T14:05:00Z",
      nonce: "result-nonce",
      signature: "result-signature"
    }
  };

  await store.start();
  try {
    assert.equal(await store.reserveNonce(issuer, nonce, "hash-1"), "new");
    assert.equal(await store.reserveNonce(issuer, nonce, "hash-1"), "duplicate");
    assert.equal(await store.reserveNonce(issuer, nonce, "hash-2"), "conflict");

    const created = await store.createTask(issuer, taskId, "hash-1", taskPayload);
    assert.equal(created.state, "created");

    const duplicate = await store.createTask(issuer, taskId, "hash-1", taskPayload);
    assert.equal(duplicate.state, "duplicate");

    await store.saveResult(issuer, taskId, resultPayload);
    const snapshot = await store.statusSnapshot(issuer, taskId);
    assert.deepEqual(snapshot.result?.output, { echo: "hello" });
    assert.equal((snapshot.task?._store as { status?: string } | undefined)?.status, "success");
  } finally {
    await store.close();
  }
});
