"""Domain errors raised by services; never carry client text or reply bodies."""


class DomainError(Exception):
    """Base class for expected, business-level failures."""
