export type QualityTier = "draft" | "standard" | "high";
export type ResponseModeKind = "sync" | "callback";
export type ResultStatus = "success" | "failure";
export type StepKind = "validate" | "plan" | "model_call" | "tool_call" | "repair" | "finalize";

export interface AuthBlock {
  alg: "Ed25519";
  key_id: string;
  issued_at: string;
  expires_at: string;
  nonce: string;
  signature: string;
}

export interface CallbackConfig {
  url: string;
  timeout_ms: number;
}

export interface ResponseMode {
  mode: ResponseModeKind;
  sync_timeout_ms?: number;
  callback?: CallbackConfig;
}

export interface TaskConstraints {
  deadline_at: string;
  max_runtime_ms: number;
  max_cost_usd: number;
  min_confidence: number;
  quality_tier: QualityTier;
}

export interface TaskEnvelope {
  protocol_version: "alp.v1";
  task_id: string;
  task_type: string;
  issuer: string;
  recipient: string;
  created_at: string;
  objective: string;
  inputs: Record<string, unknown>;
  constraints: TaskConstraints;
  expected_output_schema: Record<string, unknown>;
  response_mode: ResponseMode;
  auth: AuthBlock;
}

export interface TaskReceipt {
  protocol_version: "alp.v1";
  task_id: string;
  state: "accepted" | "duplicate";
  accepted_at: string;
  status_url: string;
}

export interface CostTracking {
  currency: "USD";
  total_usd: number;
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
}

export interface TraceStep {
  index: number;
  kind: StepKind;
  provider: string | null;
  model: string | null;
  status: ResultStatus;
  duration_ms: number;
}

export interface ExecutionTrace {
  worker_id: string;
  received_at: string;
  started_at: string;
  completed_at: string;
  attempts: number;
  steps: TraceStep[];
}

export interface ResultError {
  code:
    | "VALIDATION_ERROR"
    | "UNSUPPORTED_SCHEMA"
    | "UNAUTHORIZED"
    | "REPLAY_DETECTED"
    | "TASK_ID_CONFLICT"
    | "CAPACITY_EXCEEDED"
    | "REMOTE_TIMEOUT"
    | "EXECUTION_ERROR"
    | "OUTPUT_SCHEMA_MISMATCH"
    | "CALLBACK_DELIVERY_FAILED";
  message: string;
  retriable: boolean;
  details: Record<string, unknown>;
}

export interface ResultContract<T = unknown> {
  protocol_version: "alp.v1";
  task_id: string;
  issuer: string;
  status: ResultStatus;
  output?: T | null;
  confidence: number;
  cost: CostTracking;
  trace: ExecutionTrace;
  error?: ResultError | null;
  auth: AuthBlock;
}

export interface RetryPolicy {
  maxAttempts: number;
  baseDelayMs: number;
  maxDelayMs: number;
  jitterRatio: number;
}

export interface PeerConfig {
  agent_id: string;
  base_url: string;
  public_keys: Record<string, string>;
  allowed_task_types?: string[];
  max_schema_bytes?: number;
  max_sync_timeout_ms?: number;
  callback_domain_allowlist?: string[];
}

export interface TaskExecutor {
  canHandle(task: TaskEnvelope): Promise<boolean> | boolean;
  execute(task: TaskEnvelope): Promise<Record<string, unknown>>;
}

