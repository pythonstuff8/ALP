export class ALPError extends Error {}

export class ALPValidationError extends ALPError {}

export class ALPTransportError extends ALPError {}

export class ALPAuthError extends ALPError {}

export class ALPTimeoutError extends ALPTransportError {}

export class ALPRemoteExecutionError extends ALPError {
  readonly code: string;
  readonly retriable: boolean;

  constructor(code: string, message: string, retriable = false) {
    super(message);
    this.code = code;
    this.retriable = retriable;
  }
}

export class ALPExecutionError extends ALPError {
  readonly code: string;
  readonly retriable: boolean;
  readonly details: Record<string, unknown>;

  constructor(code: string, message: string, retriable = false, details: Record<string, unknown> = {}) {
    super(message);
    this.code = code;
    this.retriable = retriable;
    this.details = details;
  }
}

