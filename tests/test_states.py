import pytest

from app.domain.states import (
    ALLOWED_SEND_TRANSITIONS,
    ALLOWED_TRANSITIONS,
    LIVE_SEND_STATUSES,
    TERMINAL_STATUSES,
    InvalidTransition,
    MessageStatus,
    SendStatus,
    ensure_send_transition,
    ensure_transition,
)

# Restated from the contract table, independently of ALLOWED_TRANSITIONS.
EXPECTED_MESSAGE = {
    "pending": {"assigned", "awaiting_review", "cancelled"},
    "assigned": {"assigned", "processing", "awaiting_review", "cancelled"},
    "processing": {"assigned", "awaiting_review", "answered", "failed", "cancelled"},
    "awaiting_review": {"assigned", "answered", "cancelled", "pending"},
    "failed": {"cancelled", "pending"},
    "answered": set(),
    "cancelled": set(),
}
EXPECTED_SEND = {
    "queued": {"sending", "failed"},
    "sending": {"sent", "uncertain", "queued", "failed"},
    "uncertain": {"sent", "failed"},
    "sent": set(),
    "failed": set(),
}


def _pairs(table: dict[str, set[str]]) -> list[tuple[str, str, bool]]:
    return [(a, b, b in table[a]) for a in table for b in table]


def test_status_vocabularies_match_contract() -> None:
    assert {s.value for s in MessageStatus} == set(EXPECTED_MESSAGE)
    assert {s.value for s in SendStatus} == set(EXPECTED_SEND)
    assert set(ALLOWED_TRANSITIONS) == set(MessageStatus)
    assert set(ALLOWED_SEND_TRANSITIONS) == set(SendStatus)
    assert {MessageStatus.ANSWERED, MessageStatus.CANCELLED} == TERMINAL_STATUSES
    assert {s.value for s in LIVE_SEND_STATUSES} == {"queued", "sending", "sent", "uncertain"}


@pytest.mark.parametrize(("current", "target", "allowed"), _pairs(EXPECTED_MESSAGE))
def test_message_transition_matrix(current: str, target: str, allowed: bool) -> None:
    if allowed:
        assert ensure_transition(current, target) is MessageStatus(target)
        assert ensure_transition(MessageStatus(current), MessageStatus(target)) is MessageStatus(
            target
        )
    else:
        with pytest.raises(InvalidTransition) as exc:
            ensure_transition(current, target)
        assert (exc.value.current, exc.value.target) == (current, target)


@pytest.mark.parametrize(("current", "target", "allowed"), _pairs(EXPECTED_SEND))
def test_send_transition_matrix(current: str, target: str, allowed: bool) -> None:
    if allowed:
        assert ensure_send_transition(current, target) is SendStatus(target)
    else:
        with pytest.raises(InvalidTransition):
            ensure_send_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"), [("pending", "bogus"), ("bogus", "pending"), ("", "")]
)
def test_unknown_status_is_invalid_transition(current: str, target: str) -> None:
    with pytest.raises(InvalidTransition):
        ensure_transition(current, target)
    with pytest.raises(InvalidTransition):
        ensure_send_transition(current, target)
