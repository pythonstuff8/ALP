import { RetryPolicy } from "./types.js";

export async function withRetry<T>(
  operation: () => Promise<T>,
  policy: RetryPolicy,
  shouldRetry: (error: unknown) => boolean
): Promise<T> {
  let delayMs = policy.baseDelayMs;
  let lastError: unknown;
  for (let attempt = 1; attempt <= policy.maxAttempts; attempt += 1) {
    try {
      return await operation();
    } catch (error) {
      lastError = error;
      if (attempt >= policy.maxAttempts || !shouldRetry(error)) {
        throw error;
      }
      const jitter = delayMs * policy.jitterRatio;
      const waitMs = Math.max(0, delayMs + (Math.random() * 2 - 1) * jitter);
      await new Promise((resolve) => setTimeout(resolve, waitMs));
      delayMs = Math.min(delayMs * 2, policy.maxDelayMs);
    }
  }
  throw lastError;
}

