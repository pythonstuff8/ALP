import resultContractSchema from "../../schemas/result-contract.v1.json" with { type: "json" };
import taskEnvelopeSchema from "../../schemas/task-envelope.v1.json" with { type: "json" };
import taskReceiptSchema from "../../schemas/task-receipt.v1.json" with { type: "json" };

export const TASK_ENVELOPE_SCHEMA = taskEnvelopeSchema;
export const RESULT_CONTRACT_SCHEMA = resultContractSchema;
export const TASK_RECEIPT_SCHEMA = taskReceiptSchema;
