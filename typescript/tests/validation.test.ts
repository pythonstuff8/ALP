import assert from "node:assert/strict";
import test from "node:test";

import { ALPValidationError } from "../src/errors.js";
import { validateExpectedOutputSchema } from "../src/validator.js";

test("output schema rejects oneOf", () => {
  assert.throws(
    () =>
      validateExpectedOutputSchema({
        schema_dialect: "alp.output-schema.v1",
        schema: {
          type: "object",
          properties: {
            value: {
              oneOf: [{ type: "string" }, { type: "number" }]
            }
          },
          required: ["value"],
          additionalProperties: false
        }
      }),
    ALPValidationError
  );
});

