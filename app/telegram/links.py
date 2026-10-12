"""Links to a lead's source found in work-chat messages (t.me/..., tg://...)."""

from dataclasses import dataclass
from typing import Literal

LinkKind = Literal["message", "user"]


@dataclass(frozen=True, slots=True, kw_only=True)
class LeadLink:
    """A parsed link: a message in a group (`kind='message'`: chat_username or
    chat_internal_id from t.me/c/<id>, plus message_id) or a user profile
    (`kind='user'`: username or user_id). `url` is the link as found."""

    kind: LinkKind
    chat_username: str | None = None
    chat_internal_id: int | None = None
    message_id: int | None = None
    username: str | None = None
    user_id: int | None = None
    url: str
