import pytest

from alp.errors import ALPValidationError
from alp.validator import validate_expected_output_schema


def test_output_schema_rejects_oneof() -> None:
    with pytest.raises(ALPValidationError):
        validate_expected_output_schema(
            {
                "schema_dialect": "alp.output-schema.v1",
                "schema": {
                    "type": "object",
                    "properties": {"value": {"oneOf": [{"type": "string"}, {"type": "number"}]}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            }
        )

