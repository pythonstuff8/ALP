from alp.crypto import generate_keypair, sign_protocol_object, verify_protocol_object


def test_sign_and_verify_roundtrip() -> None:
    private_key, public_key = generate_keypair()
    payload = {
        "protocol_version": "alp.v1",
        "task_id": "tsk_01JQ8Y0A6K4W8X7R9M1N2P3Q",
        "task_type": "com.acme.echo.v1",
        "issuer": "sender-a",
        "recipient": "receiver-b",
        "created_at": "2026-03-28T14:00:00Z",
        "objective": "echo",
        "inputs": {"hello": "world"},
        "constraints": {
            "deadline_at": "2026-03-28T14:05:00Z",
            "max_runtime_ms": 5000,
            "max_cost_usd": 0.5,
            "min_confidence": 0.5,
            "quality_tier": "standard",
        },
        "expected_output_schema": {
            "schema_dialect": "alp.output-schema.v1",
            "schema": {
                "type": "object",
                "properties": {"hello": {"type": "string"}},
                "required": ["hello"],
                "additionalProperties": False,
            },
        },
        "response_mode": {"mode": "sync", "sync_timeout_ms": 5000},
        "auth": {"alg": "Ed25519", "key_id": "demo-key", "issued_at": "", "expires_at": "", "nonce": "", "signature": ""},
    }
    signed = sign_protocol_object(payload, private_key)
    verify_protocol_object(signed, public_key)

