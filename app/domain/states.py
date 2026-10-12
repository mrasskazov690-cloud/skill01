"""Status vocabularies and the message status transition table."""

from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType

from app.domain.errors import DomainError


class MessageStatus(StrEnum):
    PENDING = "pending"
    ASSIGNED = "assigned"
    PROCESSING = "processing"
    AWAITING_REVIEW = "awaiting_review"
    ANSWERED = "answered"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SendStatus(StrEnum):
    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


class AccountKind(StrEnum):
    MAIN = "main"
    OPERATOR = "operator"
    BOT = "bot"


class AccountState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    FLOOD_WAIT = "flood_wait"
    RESTRICTED = "restricted"
    DISCONNECTED = "disconnected"


class ConversationStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


class MessageSource(StrEnum):
    DIRECT = "direct"
    WORK_CHAT = "work_chat"


class AssignmentReason(StrEnum):
    AUTO_ROUND_ROBIN = "auto_round_robin"
    PINNED = "pinned"
    MANUAL_ASSIGN = "manual_assign"
    MANUAL_REASSIGN = "manual_reassign"


class OutboxKind(StrEnum):
    TEMPLATE = "template"
    MANUAL = "manual"


class DraftSource(StrEnum):
    TEMPLATE = "template"
    LLM = "llm"
    MANUAL = "manual"


class DraftStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


_M = MessageStatus
ALLOWED_TRANSITIONS: Mapping[MessageStatus, frozenset[MessageStatus]] = MappingProxyType(
    {
        _M.PENDING: frozenset({_M.ASSIGNED, _M.AWAITING_REVIEW, _M.CANCELLED}),
        _M.ASSIGNED: frozenset({_M.ASSIGNED, _M.PROCESSING, _M.AWAITING_REVIEW, _M.CANCELLED}),
        _M.PROCESSING: frozenset({_M.ASSIGNED, _M.AWAITING_REVIEW, _M.ANSWERED, _M.FAILED}),
        _M.AWAITING_REVIEW: frozenset({_M.ASSIGNED, _M.ANSWERED, _M.CANCELLED, _M.PENDING}),
        _M.FAILED: frozenset({_M.CANCELLED, _M.PENDING}),
        _M.ANSWERED: frozenset(),
        _M.CANCELLED: frozenset(),
    }
)
TERMINAL_STATUSES = frozenset(s for s, targets in ALLOWED_TRANSITIONS.items() if not targets)
# Messages that count against accounts.max_active.
ACTIVE_STATUSES = frozenset({_M.ASSIGNED, _M.PROCESSING})

_S = SendStatus
ALLOWED_SEND_TRANSITIONS: Mapping[SendStatus, frozenset[SendStatus]] = MappingProxyType(
    {
        _S.QUEUED: frozenset({_S.SENDING, _S.FAILED}),
        # sending -> queued: FLOOD_WAIT, the server refused, nothing was sent.
        _S.SENDING: frozenset({_S.SENT, _S.UNCERTAIN, _S.QUEUED, _S.FAILED}),
        _S.UNCERTAIN: frozenset({_S.SENT, _S.FAILED}),
        _S.SENT: frozenset(),
        _S.FAILED: frozenset(),
    }
)
# Must match the partial unique index uq_outbox_live_message (at most one per message).
LIVE_SEND_STATUSES = frozenset({_S.QUEUED, _S.SENDING, _S.SENT, _S.UNCERTAIN})


class InvalidTransition(DomainError):
    def __init__(self, current: object, target: object, entity: str = "message") -> None:
        super().__init__(f"{entity}: transition {current!s} -> {target!s} is not allowed")
        self.current = current
        self.target = target
        self.entity = entity


def can_transition(current: MessageStatus | str, target: MessageStatus | str) -> bool:
    try:
        return MessageStatus(target) in ALLOWED_TRANSITIONS[MessageStatus(current)]
    except ValueError:
        return False


def ensure_transition(current: MessageStatus | str, target: MessageStatus | str) -> MessageStatus:
    """Return `target` as MessageStatus or raise InvalidTransition (also for unknown values)."""
    if not can_transition(current, target):
        raise InvalidTransition(current, target)
    return MessageStatus(target)


def ensure_send_transition(current: SendStatus | str, target: SendStatus | str) -> SendStatus:
    try:
        allowed = SendStatus(target) in ALLOWED_SEND_TRANSITIONS[SendStatus(current)]
    except ValueError:
        allowed = False
    if not allowed:
        raise InvalidTransition(current, target, entity="outbox")
    return SendStatus(target)
