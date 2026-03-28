import { signProtocolObject, signStatusRequest, verifyProtocolObject } from "./crypto.js";
import {
  ALPAuthError,
  ALPRemoteExecutionError,
  ALPTimeoutError,
  ALPTransportError,
  ALPValidationError
} from "./errors.js";
import { withRetry } from "./retry.js";
import { TrustStore } from "./trust.js";
import { PeerConfig, ResultContract, RetryPolicy, TaskEnvelope, TaskReceipt } from "./types.js";
import {
  validateExpectedOutputSchema,
  validateOutputAgainstSchema,
  validateResultContract,
  validateTaskEnvelope,
  validateTaskReceipt
} from "./validator.js";

const DEFAULT_RETRY_POLICY: RetryPolicy = {
  maxAttempts: 4,
  baseDelayMs: 250,
  maxDelayMs: 2000,
  jitterRatio: 0.2
};

export class ALPClient {
  readonly trustStore: TrustStore;

  constructor(
    private readonly cfg: {
      issuer: string;
      keyId: string;
      privateKey: Uint8Array | string;
      defaultTimeoutMs?: number;
      trustStore?: TrustStore;
    }
  ) {
    this.trustStore = cfg.trustStore ?? new TrustStore();
  }

  validateTask(task: TaskEnvelope): void {
    validateTaskEnvelope(task);
  }

  validateResult<T>(result: ResultContract<T>, expectedOutputSchema: object): void {
    validateResultContract(result);
    if (result.status === "success") {
      validateOutputAgainstSchema(result.output, expectedOutputSchema as Record<string, unknown>);
    }
  }

  async submit<T = unknown>(
    peer: PeerConfig,
    task: TaskEnvelope,
    opts?: { retry?: RetryPolicy }
  ): Promise<TaskReceipt | ResultContract<T>> {
    if (task.issuer !== this.cfg.issuer) {
      throw new ALPValidationError("task issuer does not match client issuer");
    }
    if (task.recipient !== peer.agent_id) {
      throw new ALPValidationError("task recipient does not match selected peer");
    }
    validateExpectedOutputSchema(task.expected_output_schema);
    const signedTask = signProtocolObject(
      {
        ...structuredClone(task),
        auth: { ...structuredClone(task.auth), key_id: this.cfg.keyId }
      },
      this.cfg.privateKey
    );
    validateTaskEnvelope(signedTask);

    const operation = async (): Promise<TaskReceipt | ResultContract<T>> => {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), this.cfg.defaultTimeoutMs ?? 10000);
      try {
        const response = await fetch(`${peer.base_url}/alp/v1/tasks`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(signedTask),
          signal: controller.signal
        });
        const payload = await response.json();
        if (response.status === 200) {
          validateResultContract(payload);
          const publicKey = this.trustStore.getPublicKey(payload.issuer as string, payload.auth.key_id as string);
          verifyProtocolObject(payload as unknown as Record<string, unknown>, publicKey);
          if (payload.status === "success") {
            validateOutputAgainstSchema(payload.output, task.expected_output_schema);
          } else {
            throw new ALPRemoteExecutionError(payload.error.code, payload.error.message, payload.error.retriable);
          }
          return payload as ResultContract<T>;
        }
        if (response.status === 202) {
          validateTaskReceipt(payload);
          return payload as TaskReceipt;
        }
        if (response.status === 401 || response.status === 403) {
          throw new ALPAuthError(JSON.stringify(payload));
        }
        if (response.status === 409 || response.status === 422) {
          throw new ALPValidationError(JSON.stringify(payload));
        }
        if (response.status === 429 || response.status >= 500) {
          throw new ALPTransportError(JSON.stringify(payload));
        }
        throw new ALPTransportError(`unexpected status ${response.status}`);
      } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
          throw new ALPTimeoutError("submit timed out");
        }
        throw error;
      } finally {
        clearTimeout(timeout);
      }
    };

    return withRetry(operation, opts?.retry ?? DEFAULT_RETRY_POLICY, (error) => error instanceof ALPTransportError);
  }

  async wait<T = unknown>(
    peer: PeerConfig,
    taskId: string,
    opts?: { timeoutMs?: number; pollIntervalMs?: number }
  ): Promise<ResultContract<T>> {
    const timeoutAt = Date.now() + (opts?.timeoutMs ?? 60000);
    const path = `/alp/v1/tasks/${taskId}`;
    while (Date.now() < timeoutAt) {
      const headers = signStatusRequest(path, this.cfg.issuer, this.cfg.keyId, this.cfg.privateKey);
      const response = await fetch(`${peer.base_url}${path}`, { headers });
      if (response.status === 200) {
        const payload = (await response.json()) as ResultContract<T>;
        validateResultContract(payload);
        const publicKey = this.trustStore.getPublicKey(payload.issuer, payload.auth.key_id);
        verifyProtocolObject(payload as unknown as Record<string, unknown>, publicKey);
        return payload;
      }
      if (response.status === 202 || response.status === 404) {
        await new Promise((resolve) => setTimeout(resolve, opts?.pollIntervalMs ?? 2000));
        continue;
      }
      throw new ALPTransportError(`unexpected status ${response.status}`);
    }
    throw new ALPTimeoutError("timed out waiting for terminal result");
  }
}
