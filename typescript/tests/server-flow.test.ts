import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { ALPClient } from "../src/client.js";
import { generateKeypair } from "../src/crypto.js";
import { ALPServer } from "../src/server.js";
import { FileTaskStore } from "../src/store.js";
import { TrustStore } from "../src/trust.js";
import { PeerConfig, TaskEnvelope, TaskExecutor } from "../src/types.js";

class EchoExecutor implements TaskExecutor {
  canHandle(task: TaskEnvelope): boolean {
    return task.task_type === "com.acme.echo.v1";
  }

  async execute(task: TaskEnvelope): Promise<Record<string, unknown>> {
    return { echo: task.inputs.message as string };
  }
}

test("sync submit and deduplicate", async () => {
  const tmpDir = await mkdtemp(path.join(os.tmpdir(), "alp-ts-"));
  const senderKeys = generateKeypair();
  const receiverKeys = generateKeypair();

  const receiverTrust = new TrustStore();
  receiverTrust.addPeer({
    agent_id: "sender-a",
    public_keys: { "sender-key": senderKeys.publicKey },
    allowed_task_types: ["com.acme.echo.v1"]
  });
  const server = new ALPServer({
    agentId: "receiver-b",
    trustStore: receiverTrust,
    store: new FileTaskStore(path.join(tmpDir, "store.json")),
    executor: new EchoExecutor(),
    keyId: "receiver-key",
    privateKey: receiverKeys.privateKey
  });
  const app = server.createApp();
  await app.listen({ host: "127.0.0.1", port: 0 });
  const address = app.addresses()[0];
  const baseUrl = typeof address === "string" ? address : `http://127.0.0.1:${address.port}`;

  const clientTrust = new TrustStore();
  clientTrust.addPeer({
    agent_id: "receiver-b",
    public_keys: { "receiver-key": receiverKeys.publicKey },
    allowed_task_types: ["com.acme.echo.v1"]
  });
  const client = new ALPClient({
    issuer: "sender-a",
    keyId: "sender-key",
    privateKey: senderKeys.privateKey,
    trustStore: clientTrust
  });
  const peer: PeerConfig = {
    agent_id: "receiver-b",
    base_url: baseUrl,
    public_keys: { "receiver-key": receiverKeys.publicKey }
  };

  const task: TaskEnvelope = {
    protocol_version: "alp.v1",
    task_id: "tsk_01JQ8Y0A6K4W8X7R9M1N2P3Q",
    task_type: "com.acme.echo.v1",
    issuer: "sender-a",
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
    auth: { alg: "Ed25519", key_id: "sender-key", issued_at: "", expires_at: "", nonce: "", signature: "" }
  };

  const result = await client.submit(peer, task);
  assert.equal((result as { output: { echo: string } }).output.echo, "hello");

  const duplicate = await client.submit(peer, task);
  assert.equal((duplicate as { output: { echo: string } }).output.echo, "hello");

  await app.close();
});

