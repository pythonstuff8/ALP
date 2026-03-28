import { createHash } from "node:crypto";
import nacl from "tweetnacl";

import { canonicalJson } from "./canonical.js";
import { ALPAuthError } from "./errors.js";

export function utcNowIso(): string {
  return new Date().toISOString();
}

function b64urlEncode(bytes: Uint8Array): string {
  return Buffer.from(bytes).toString("base64url");
}

function b64urlDecode(value: string): Uint8Array {
  return new Uint8Array(Buffer.from(value, "base64url"));
}

export function generateNonce(): string {
  return b64urlEncode(nacl.randomBytes(18));
}

export function generateKeypair(): { privateKey: string; publicKey: string } {
  const pair = nacl.sign.keyPair();
  const seed = pair.secretKey.slice(0, 32);
  return {
    privateKey: b64urlEncode(seed),
    publicKey: b64urlEncode(pair.publicKey)
  };
}

function keyPairFromSeed(privateKey: string | Uint8Array) {
  const seed = typeof privateKey === "string" ? b64urlDecode(privateKey) : privateKey;
  return nacl.sign.keyPair.fromSeed(seed);
}

function payloadWithoutSignature(payload: Record<string, unknown>): Record<string, unknown> {
  const copy = structuredClone(payload);
  const auth = (copy.auth ?? {}) as Record<string, unknown>;
  auth.signature = "";
  copy.auth = auth;
  return copy;
}

export function sha256Hex(value: unknown): string {
  return createHash("sha256").update(canonicalJson(value)).digest("hex");
}

export function signProtocolObject<T extends Record<string, unknown>>(
  payload: T,
  privateKey: string | Uint8Array,
  ttlSeconds = 300
): T {
  const signed = structuredClone(payload);
  const auth = (((signed as Record<string, unknown>).auth as Record<string, unknown> | undefined) ?? {}) as Record<string, unknown>;
  auth.alg = "Ed25519";
  auth.issued_at = utcNowIso();
  auth.expires_at = new Date(Date.now() + ttlSeconds * 1000).toISOString();
  auth.nonce = typeof auth.nonce === "string" && auth.nonce.length > 0 ? auth.nonce : generateNonce();
  if (!auth.key_id || typeof auth.key_id !== "string") {
    throw new ALPAuthError("auth.key_id is required before signing");
  }
  (signed as Record<string, unknown>).auth = auth;
  const pair = keyPairFromSeed(privateKey);
  const signature = nacl.sign.detached(Buffer.from(canonicalJson(payloadWithoutSignature(signed))), pair.secretKey);
  auth.signature = b64urlEncode(signature);
  return signed;
}

export function verifyProtocolObject(payload: Record<string, unknown>, publicKey: string | Uint8Array): void {
  const auth = (payload.auth ?? {}) as Record<string, unknown>;
  const signature = auth.signature;
  if (typeof signature !== "string" || !signature) {
    throw new ALPAuthError("missing signature");
  }
  const expiresAt = auth.expires_at;
  if (typeof expiresAt !== "string" || new Date(expiresAt).getTime() < Date.now()) {
    throw new ALPAuthError("signature has expired");
  }
  const key = typeof publicKey === "string" ? b64urlDecode(publicKey) : publicKey;
  const ok = nacl.sign.detached.verify(
    Buffer.from(canonicalJson(payloadWithoutSignature(payload))),
    b64urlDecode(signature),
    key
  );
  if (!ok) {
    throw new ALPAuthError("invalid signature");
  }
}

export function signStatusRequest(
  path: string,
  issuer: string,
  keyId: string,
  privateKey: string | Uint8Array,
  ttlSeconds = 300
): Record<string, string> {
  const issuedAt = utcNowIso();
  const expiresAt = new Date(Date.now() + ttlSeconds * 1000).toISOString();
  const nonce = generateNonce();
  const message = Buffer.from(`GET\n${path}\n${issuer}\n${issuedAt}\n${expiresAt}\n${nonce}`);
  const pair = keyPairFromSeed(privateKey);
  const signature = nacl.sign.detached(message, pair.secretKey);
  return {
    "X-ALP-Issuer": issuer,
    "X-ALP-Key-Id": keyId,
    "X-ALP-Issued-At": issuedAt,
    "X-ALP-Expires-At": expiresAt,
    "X-ALP-Nonce": nonce,
    "X-ALP-Signature": b64urlEncode(signature)
  };
}

export function verifyStatusHeaders(
  path: string,
  headers: Record<string, string | string[] | undefined>,
  publicKey: string | Uint8Array
): string {
  const issuer = headers["x-alp-issuer"] as string | undefined;
  const issuedAt = headers["x-alp-issued-at"] as string | undefined;
  const expiresAt = headers["x-alp-expires-at"] as string | undefined;
  const nonce = headers["x-alp-nonce"] as string | undefined;
  const signature = headers["x-alp-signature"] as string | undefined;
  if (!issuer || !issuedAt || !expiresAt || !nonce || !signature) {
    throw new ALPAuthError("missing signed status headers");
  }
  if (new Date(expiresAt).getTime() < Date.now()) {
    throw new ALPAuthError("status signature expired");
  }
  const message = Buffer.from(`GET\n${path}\n${issuer}\n${issuedAt}\n${expiresAt}\n${nonce}`);
  const key = typeof publicKey === "string" ? b64urlDecode(publicKey) : publicKey;
  const ok = nacl.sign.detached.verify(message, b64urlDecode(signature), key);
  if (!ok) {
    throw new ALPAuthError("invalid status request signature");
  }
  return issuer;
}
