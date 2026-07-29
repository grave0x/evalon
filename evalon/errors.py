"""Evalon exception types."""


class EvalonError(Exception):
    """Base exception for Evalon errors."""


class EvalonStorageError(EvalonError):
    """Raised when Evalon cannot persist a trace."""
