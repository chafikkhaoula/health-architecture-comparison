from __future__ import annotations


class AdapterError(RuntimeError):
    """Base error shared by all architecture adapters."""


class RecordAlreadyExistsError(AdapterError):
    """Raised when OP1 targets an existing record."""


class RecordNotFoundError(AdapterError):
    """Raised when an operation targets a missing record."""


class AccessDeniedError(AdapterError):
    """Raised when OP2 is attempted without an ALLOW rule."""
