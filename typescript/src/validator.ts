import Ajv2020Module from "ajv/dist/2020.js";
import addFormatsModule from "ajv-formats";

import { ALPValidationError } from "./errors.js";
import { RESULT_CONTRACT_SCHEMA, TASK_ENVELOPE_SCHEMA, TASK_RECEIPT_SCHEMA } from "./schema.js";

const Ajv2020 = Ajv2020Module as unknown as new (opts?: Record<string, unknown>) => AjvLike;
const addFormats = addFormatsModule as unknown as (ajv: AjvLike) => void;

interface AjvLike {
  compile: (schema: Record<string, unknown>) => ((payload: unknown) => boolean) & { errors?: unknown };
}

const ajv = new Ajv2020({ allErrors: true, strict: false });
addFormats(ajv);

const validateTask = ajv.compile(TASK_ENVELOPE_SCHEMA);
const validateResult = ajv.compile(RESULT_CONTRACT_SCHEMA);
const validateReceipt = ajv.compile(TASK_RECEIPT_SCHEMA);

const ALLOWED_OUTPUT_SCHEMA_KEYS = new Set([
  "type",
  "properties",
  "required",
  "items",
  "enum",
  "const",
  "additionalProperties",
  "minItems",
  "maxItems",
  "minLength",
  "maxLength",
  "minimum",
  "maximum",
  "description"
]);
const DISALLOWED_OUTPUT_SCHEMA_KEYS = new Set(["$ref", "patternProperties", "not", "allOf", "anyOf", "oneOf"]);

function fail(prefix: string, errors: unknown): never {
  throw new ALPValidationError(`${prefix}: ${JSON.stringify(errors)}`);
}

export function validateTaskEnvelope(payload: unknown): void {
  if (!validateTask(payload)) {
    fail("task invalid", validateTask.errors);
  }
  validateExpectedOutputSchema((payload as { expected_output_schema: Record<string, unknown> }).expected_output_schema);
}

export function validateResultContract(payload: unknown): void {
  if (!validateResult(payload)) {
    fail("result invalid", validateResult.errors);
  }
}

export function validateTaskReceipt(payload: unknown): void {
  if (!validateReceipt(payload)) {
    fail("receipt invalid", validateReceipt.errors);
  }
}

export function validateExpectedOutputSchema(expectedOutputSchema: Record<string, unknown>): void {
  if (expectedOutputSchema.schema_dialect !== "alp.output-schema.v1") {
    throw new ALPValidationError("expected_output_schema.schema_dialect must be alp.output-schema.v1");
  }
  const schema = expectedOutputSchema.schema;
  if (!schema || typeof schema !== "object" || Array.isArray(schema)) {
    throw new ALPValidationError("expected_output_schema.schema must be an object");
  }
  const serialized = JSON.stringify(schema);
  if (Buffer.byteLength(serialized, "utf8") > 32768) {
    throw new ALPValidationError("expected_output_schema exceeds 32 KiB");
  }
  if ((schema as Record<string, unknown>).type !== "object") {
    throw new ALPValidationError("expected_output_schema root type must be object");
  }
  validateSchemaNode(schema, 0);
}

function validateSchemaNode(node: unknown, depth: number, insideProperties = false): void {
  if (depth > 8) {
    throw new ALPValidationError("expected_output_schema exceeds maximum depth of 8");
  }
  if (Array.isArray(node)) {
    node.forEach((item) => validateSchemaNode(item, depth + 1));
    return;
  }
  if (!node || typeof node !== "object") {
    return;
  }
  if (insideProperties) {
    Object.values(node).forEach((value) => validateSchemaNode(value, depth + 1));
    return;
  }
  for (const key of Object.keys(node)) {
    if (DISALLOWED_OUTPUT_SCHEMA_KEYS.has(key)) {
      throw new ALPValidationError(`expected_output_schema uses disallowed key ${key}`);
    }
    if (!ALLOWED_OUTPUT_SCHEMA_KEYS.has(key)) {
      throw new ALPValidationError(`expected_output_schema uses unsupported key ${key}`);
    }
    validateSchemaNode((node as Record<string, unknown>)[key], depth + 1, key === "properties");
  }
}

export function validateOutputAgainstSchema(output: unknown, expectedOutputSchema: Record<string, unknown>): void {
  validateExpectedOutputSchema(expectedOutputSchema);
  const outputValidator = ajv.compile(expectedOutputSchema.schema as Record<string, unknown>);
  if (!outputValidator(output)) {
    fail("output invalid", outputValidator.errors);
  }
}

export function validateCallbackUrl(url: string, allowlist: string[]): void {
  const parsed = new URL(url);
  if (parsed.protocol !== "https:" && parsed.hostname !== "localhost" && parsed.hostname !== "127.0.0.1") {
    throw new ALPValidationError("callback URL must be https outside local development");
  }
  if (allowlist.length > 0 && !allowlist.includes(parsed.hostname)) {
    throw new ALPValidationError("callback URL host is not allowlisted");
  }
}
