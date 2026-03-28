import assert from "node:assert/strict";
import test from "node:test";

import { generateKeypair, signProtocolObject, verifyProtocolObject } from "../src/crypto.js";

test("sign and verify roundtrip", () => {
  const { privateKey, publicKey } = generateKeypair();
  const signed = signProtocolObject(
    {
      protocol_version: "alp.v1",
      task_id: "tsk_01JQ8Y0A6K4W8X7R9M1N2P3Q",
      task_type: "com.acme.echo.v1",
      issuer: "sender-a",
      recipient: "receiver-b",
      created_at: "2026-03-28T14:00:00Z",
      objective: "echo",
      inputs: { hello: "world" },
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
          properties: { hello: { type: "string" } },
          required: ["hello"],
          additionalProperties: false
        }
      },
      response_mode: { mode: "sync", sync_timeout_ms: 5000 },
      auth: { alg: "Ed25519", key_id: "demo-key", issued_at: "", expires_at: "", nonce: "", signature: "" }
    },
    privateKey
  );
  assert.doesNotThrow(() => verifyProtocolObject(signed, publicKey));
});

