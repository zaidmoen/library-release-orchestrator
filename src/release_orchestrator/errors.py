"""Exception hierarchy. Each error maps to a stable CLI exit code and machine-readable code."""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    INTERNAL_ERROR = 1
    INVALID_INPUT = 2
    BLOCKED = 3
    APPROVAL_REQUIRED = 4
    AMBIGUOUS_OR_NOT_FOUND = 5
    OPERATION_FAILED = 6


class OrchestratorError(Exception):
    """Base class. ``code`` is a stable identifier used in JSON output."""

    code = "ERROR"
    exit_code = ExitCode.INTERNAL_ERROR

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigError(OrchestratorError):
    code = "INVALID_CONFIG"
    exit_code = ExitCode.INVALID_INPUT


class InvalidInputError(OrchestratorError):
    code = "INVALID_INPUT"
    exit_code = ExitCode.INVALID_INPUT


class ManifestError(OrchestratorError):
    code = "INVALID_MANIFEST"
    exit_code = ExitCode.INVALID_INPUT


class AmbiguousRequestError(OrchestratorError):
    code = "AMBIGUOUS_REQUEST"
    exit_code = ExitCode.AMBIGUOUS_OR_NOT_FOUND


class NotFoundError(OrchestratorError):
    code = "NOT_FOUND"
    exit_code = ExitCode.AMBIGUOUS_OR_NOT_FOUND


class GitError(OrchestratorError):
    code = "GIT_ERROR"
    exit_code = ExitCode.OPERATION_FAILED


class ForbiddenOperationError(OrchestratorError):
    code = "FORBIDDEN_OPERATION"
    exit_code = ExitCode.INVALID_INPUT


class ApprovalRequiredError(OrchestratorError):
    code = "APPROVAL_REQUIRED"
    exit_code = ExitCode.APPROVAL_REQUIRED


class BlockedError(OrchestratorError):
    """A safety rule stopped the workflow. Requires human action or a re-plan."""

    code = "BLOCKED"
    exit_code = ExitCode.BLOCKED

    def __init__(self, message: str, *, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, details=details)
        self.reason = reason

    def to_dict(self) -> dict[str, Any]:
        data = super().to_dict()
        data["reason"] = self.reason
        return data


class ImmutableVersionError(BlockedError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message, reason="IMMUTABLE_VERSION", details=details)


class InvalidTransitionError(OrchestratorError):
    code = "INVALID_STATE_TRANSITION"
    exit_code = ExitCode.BLOCKED


class IntegrationError(OrchestratorError):
    code = "INTEGRATION_ERROR"
    exit_code = ExitCode.OPERATION_FAILED


class ConcurrentModificationError(OrchestratorError):
    """Another process changed the same run; reload and retry (optimistic concurrency)."""

    code = "CONCURRENT_MODIFICATION"
    exit_code = ExitCode.OPERATION_FAILED
