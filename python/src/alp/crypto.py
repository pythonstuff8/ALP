from __future__ import annotations

import base64
import hashlib
import secrets
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .canonical import canonical_json_bytes
from .errors import ALPAuthError


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def parse_iso8601(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(UTC)


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def generate_nonce() -> str:
    return b64url_encode(secrets.token_bytes(18))


def generate_keypair() -> tuple[str, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_raw = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return b64url_encode(private_raw), b64url_encode(public_raw)


def load_private_key(key_material: str | bytes) -> Ed25519PrivateKey:
    if isinstance(key_material, bytes):
        raw = key_material
    else:
        raw = b64url_decode(key_material)
    return Ed25519PrivateKey.from_private_bytes(raw)


def load_public_key(key_material: str | bytes) -> Ed25519PublicKey:
    if isinstance(key_material, bytes):
        raw = key_material
    else:
        raw = b64url_decode(key_material)
    return Ed25519PublicKey.from_public_bytes(raw)


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def payload_without_signature(payload: dict[str, Any]) -> dict[str, Any]:
    clean = deepcopy(payload)
    auth = clean.get("auth")
    if isinstance(auth, dict):
        auth["signature"] = ""
    return clean


def sign_protocol_object(payload: dict[str, Any], private_key_material: str | bytes, *, ttl_seconds: int = 300) -> dict[str, Any]:
    signed = deepcopy(payload)
    auth = signed.setdefault("auth", {})
    auth.setdefault("alg", "Ed25519")
    if not auth.get("nonce"):
        auth["nonce"] = generate_nonce()
    auth["issued_at"] = utc_now_iso()
    auth["expires_at"] = (utc_now() + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
    if not auth.get("key_id"):
        raise ALPAuthError("auth.key_id is required before signing")
    unsigned = payload_without_signature(signed)
    signature = load_private_key(private_key_material).sign(canonical_json_bytes(unsigned))
    auth["signature"] = b64url_encode(signature)
    return signed


def verify_protocol_object(payload: dict[str, Any], public_key_material: str | bytes, *, now: datetime | None = None) -> None:
    auth = payload.get("auth") or {}
    signature = auth.get("signature")
    if not signature:
        raise ALPAuthError("missing signature")
    issued_at = auth.get("issued_at")
    expires_at = auth.get("expires_at")
    if not issued_at or not expires_at:
        raise ALPAuthError("missing signature timestamps")
    reference_time = now or utc_now()
    if parse_iso8601(issued_at) > reference_time + timedelta(minutes=5):
        raise ALPAuthError("signature issued_at is in the future")
    if parse_iso8601(expires_at) < reference_time:
        raise ALPAuthError("signature has expired")
    try:
        load_public_key(public_key_material).verify(b64url_decode(signature), canonical_json_bytes(payload_without_signature(payload)))
    except InvalidSignature as exc:
        raise ALPAuthError("invalid signature") from exc


def sign_status_request(path: str, issuer: str, key_id: str, private_key_material: str | bytes, *, ttl_seconds: int = 300) -> dict[str, str]:
    issued_at = utc_now_iso()
    expires_at = (utc_now() + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z")
    nonce = generate_nonce()
    message = f"GET\n{path}\n{issuer}\n{issued_at}\n{expires_at}\n{nonce}".encode("utf-8")
    signature = load_private_key(private_key_material).sign(message)
    return {
        "X-ALP-Issuer": issuer,
        "X-ALP-Key-Id": key_id,
        "X-ALP-Issued-At": issued_at,
        "X-ALP-Expires-At": expires_at,
        "X-ALP-Nonce": nonce,
        "X-ALP-Signature": b64url_encode(signature),
    }


def verify_status_headers(path: str, headers: dict[str, str], public_key_material: str | bytes, *, now: datetime | None = None) -> str:
    try:
        issuer = headers["X-ALP-Issuer"]
        issued_at = headers["X-ALP-Issued-At"]
        expires_at = headers["X-ALP-Expires-At"]
        nonce = headers["X-ALP-Nonce"]
        signature = headers["X-ALP-Signature"]
    except KeyError as exc:
        raise ALPAuthError(f"missing status header: {exc.args[0]}") from exc
    reference_time = now or utc_now()
    if parse_iso8601(expires_at) < reference_time:
        raise ALPAuthError("status request signature has expired")
    message = f"GET\n{path}\n{issuer}\n{issued_at}\n{expires_at}\n{nonce}".encode("utf-8")
    try:
        load_public_key(public_key_material).verify(b64url_decode(signature), message)
    except InvalidSignature as exc:
        raise ALPAuthError("invalid status request signature") from exc
    return issuer
