from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse

from jsonschema import Draft202012Validator, FormatChecker

from .errors import ALPValidationError
from .schema import RESULT_CONTRACT_SCHEMA, TASK_ENVELOPE_SCHEMA, TASK_RECEIPT_SCHEMA

FORMAT_CHECKER = FormatChecker()
TASK_ENVELOPE_VALIDATOR = Draft202012Validator(TASK_ENVELOPE_SCHEMA, format_checker=FORMAT_CHECKER)
RESULT_CONTRACT_VALIDATOR = Draft202012Validator(RESULT_CONTRACT_SCHEMA, format_checker=FORMAT_CHECKER)
TASK_RECEIPT_VALIDATOR = Draft202012Validator(TASK_RECEIPT_SCHEMA, format_checker=FORMAT_CHECKER)

ALLOWED_OUTPUT_SCHEMA_KEYS = {
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
    "description",
}
DISALLOWED_OUTPUT_SCHEMA_KEYS = {"$ref", "patternProperties", "not", "allOf", "anyOf", "oneOf"}


def _raise_validation_error(errors: Iterable[Any], prefix: str) -> None:
    messages = []
    for error in errors:
        location = ".".join(str(piece) for piece in error.path)
        messages.append(f"{prefix}{location or '$'}: {error.message}")
    raise ALPValidationError("; ".join(messages))


def validate_task_envelope(payload: dict[str, Any]) -> None:
    errors = list(TASK_ENVELOPE_VALIDATOR.iter_errors(payload))
    if errors:
        _raise_validation_error(errors, "task ")
    validate_expected_output_schema(payload["expected_output_schema"])


def validate_result_contract(payload: dict[str, Any]) -> None:
    errors = list(RESULT_CONTRACT_VALIDATOR.iter_errors(payload))
    if errors:
        _raise_validation_error(errors, "result ")


def validate_task_receipt(payload: dict[str, Any]) -> None:
    errors = list(TASK_RECEIPT_VALIDATOR.iter_errors(payload))
    if errors:
        _raise_validation_error(errors, "receipt ")


def validate_expected_output_schema(expected_output_schema: dict[str, Any]) -> None:
    if expected_output_schema.get("schema_dialect") != "alp.output-schema.v1":
        raise ALPValidationError("expected_output_schema.schema_dialect must be alp.output-schema.v1")
    schema = expected_output_schema.get("schema")
    if not isinstance(schema, dict):
        raise ALPValidationError("expected_output_schema.schema must be an object")
    raw = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    if len(raw.encode("utf-8")) > 32768:
        raise ALPValidationError("expected_output_schema exceeds 32 KiB")
    if schema.get("type") != "object":
        raise ALPValidationError("expected_output_schema root type must be object")
    _validate_output_schema_node(schema, depth=0)


def _validate_output_schema_node(node: Any, *, depth: int, inside_properties: bool = False) -> None:
    if depth > 8:
        raise ALPValidationError("expected_output_schema exceeds maximum depth of 8")
    if isinstance(node, dict):
        if inside_properties:
            for value in node.values():
                _validate_output_schema_node(value, depth=depth + 1)
            return
        for key, value in node.items():
            if key in DISALLOWED_OUTPUT_SCHEMA_KEYS:
                raise ALPValidationError(f"expected_output_schema uses disallowed key {key}")
            if key not in ALLOWED_OUTPUT_SCHEMA_KEYS:
                raise ALPValidationError(f"expected_output_schema uses unsupported key {key}")
            _validate_output_schema_node(value, depth=depth + 1, inside_properties=(key == "properties"))
    elif isinstance(node, list):
        for item in node:
            _validate_output_schema_node(item, depth=depth + 1)


def validate_output_against_schema(output: Any, expected_output_schema: dict[str, Any]) -> None:
    validate_expected_output_schema(expected_output_schema)
    schema = expected_output_schema["schema"]
    validator = Draft202012Validator(schema, format_checker=FORMAT_CHECKER)
    errors = list(validator.iter_errors(output))
    if errors:
        _raise_validation_error(errors, "output ")


def validate_callback_url(url: str, allowlist: list[str]) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ALPValidationError("callback URL must be https outside local development")
    if allowlist and parsed.hostname not in set(allowlist):
        raise ALPValidationError("callback URL host is not allowlisted")
