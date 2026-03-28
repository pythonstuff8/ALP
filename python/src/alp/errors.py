class ALPError(Exception):
    """Base error for all ALP failures."""


class ALPValidationError(ALPError):
    """Raised when a payload or schema is invalid."""


class ALPTransportError(ALPError):
    """Raised when HTTP transport fails."""


class ALPAuthError(ALPError):
    """Raised when signing or verification fails."""


class ALPTimeoutError(ALPTransportError):
    """Raised when the remote side does not respond in time."""


class ALPRemoteExecutionError(ALPError):
    """Raised when a remote peer returns a terminal failure result."""

    def __init__(self, code: str, message: str, retriable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retriable = retriable


class ALPExecutionError(ALPError):
    """Raised by local executors to return a structured failure result."""

    def __init__(self, code: str, message: str, retriable: bool = False, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retriable = retriable
        self.details = details or {}

