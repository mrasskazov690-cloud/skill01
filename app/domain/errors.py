"""Domain errors raised by services; never carry client text or reply bodies."""


class DomainError(Exception):
    """Base class for expected, business-level failures."""


class NotFound(DomainError):
    """The referenced message, conversation, account or outbox row does not exist."""


class AccountNotAssignable(DomainError):
    """Manual assignment target is not an enabled operator account."""


class ConversationBusy(DomainError):
    """The conversation has a message in processing or a live send; it cannot move now."""


class NotOwner(DomainError):
    """The caller is not the operator (or claim holder) of this message."""


class WrongState(DomainError):
    """The entity's current status does not allow this operation."""


class SendInFlight(DomainError):
    """The message already has a live outbox row (queued, sending, sent or uncertain)."""


class IdempotencyConflict(DomainError):
    """The idempotency key is already used by an outbox row of another message."""
