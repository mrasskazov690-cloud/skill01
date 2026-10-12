"""Links to a lead's source found in work-chat messages (t.me/..., tg://...).

Pure functions: no network, no client. Entity offsets and lengths are in UTF-16 code
units, as Telegram counts them, so an emoji before a link shifts it by two units.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl, TypeMessageEntity

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


_HOSTS = frozenset({"t.me", "www.t.me", "telegram.me", "www.telegram.me"})
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_NUMBER = re.compile(r"[1-9][0-9]{0,18}")
# Telegram's widths: message ids are int32, user and channel ids int64. Larger numbers
# would fail later (request serialization, bigint columns) and stall intake on one lead.
_MAX_MESSAGE_ID = 2**31 - 1
_MAX_PEER_ID = 2**63 - 1


def _number(value: str, maximum: int) -> int | None:
    if not _NUMBER.fullmatch(value):
        return None
    number = int(value)
    return number if number <= maximum else None


# First path segments of t.me that are services, not usernames (invites, stickers, ...).
_RESERVED = frozenset(
    {
        "addemoji",
        "addlist",
        "addstickers",
        "addtheme",
        "boost",
        "contact",
        "giftcode",
        "invoice",
        "iv",
        "joinchat",
        "login",
        "m",
        "proxy",
        "setlanguage",
        "share",
        "socks",
    }
)
# Links in plain text that Telegram did not mark with an entity (tg:// is never marked).
_TEXT_LINK = re.compile(
    r"(?<![\w/.@])(?:tg://[^\s<>\"']+|(?:https?://)?(?:www\.)?(?:t|telegram)\.me/[^\s<>\"']+)",
    re.IGNORECASE,
)


def parse_link(url: str) -> LeadLink | None:
    """Parse one URL; None if it is not a link to a Telegram message or user."""
    url = raw = url.strip().rstrip(".,;:!?)»")
    if not raw:
        return None
    if raw.lower().startswith("tg://"):
        return _parse_tg(raw)
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
        hostname = parts.hostname or ""
    except ValueError:  # e.g. an unbalanced "[" read as an IPv6 host
        return None
    if parts.scheme.lower() not in ("http", "https") or hostname not in _HOSTS:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if segments and segments[0].lower() == "s":  # t.me/s/<channel>/<msg>: web preview
        segments = segments[1:]
    if not segments or segments[0].lower() in _RESERVED or segments[0].startswith("+"):
        return None
    if segments[0].lower() == "c":
        # t.me/c/<internal id>/<msg> or t.me/c/<internal id>/<topic>/<msg>
        if len(segments) in (3, 4) and all(_NUMBER.fullmatch(s) for s in segments[1:]):
            chat_id = _number(segments[1], _MAX_PEER_ID)
            message_id = _number(segments[-1], _MAX_MESSAGE_ID)
            if chat_id is not None and message_id is not None:
                return LeadLink(
                    kind="message", chat_internal_id=chat_id, message_id=message_id, url=url
                )
        return None
    name = segments[0].removeprefix("@")
    if not _USERNAME.fullmatch(name):
        return None
    if len(segments) == 1:
        return LeadLink(kind="user", username=name, url=url)
    # t.me/<group>/<msg> or t.me/<group>/<topic>/<msg>
    if len(segments) in (2, 3) and all(_NUMBER.fullmatch(s) for s in segments[1:]):
        message_id = _number(segments[-1], _MAX_MESSAGE_ID)
        if message_id is not None:
            return LeadLink(kind="message", chat_username=name, message_id=message_id, url=url)
    return None


def _parse_tg(url: str) -> LeadLink | None:
    try:
        parts = urlsplit(url)
        query = {k.lower(): v[0] for k, v in parse_qs(parts.query).items()}
    except ValueError:
        return None
    action = (parts.netloc or parts.path.strip("/")).lower()
    post = query.get("post", "")
    match action:
        case "user":
            user_id = _number(query.get("id", ""), _MAX_PEER_ID)
            if user_id is not None:
                return LeadLink(kind="user", user_id=user_id, url=url)
        case "resolve":
            domain = query.get("domain", "")
            if not _USERNAME.fullmatch(domain):
                return None
            if not post:
                return LeadLink(kind="user", username=domain, url=url)
            message_id = _number(post, _MAX_MESSAGE_ID)
            if message_id is not None:
                return LeadLink(
                    kind="message", chat_username=domain, message_id=message_id, url=url
                )
        case "privatepost":
            channel = _number(query.get("channel", ""), _MAX_PEER_ID)
            message_id = _number(post, _MAX_MESSAGE_ID)
            if channel is not None and message_id is not None:
                return LeadLink(
                    kind="message", chat_internal_id=channel, message_id=message_id, url=url
                )
    return None


def _utf16_slice(encoded: bytes, offset: int, length: int) -> str:
    return encoded[offset * 2 : (offset + length) * 2].decode("utf-16-le", errors="ignore")


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def extract_links(text: str | None, entities: Sequence[TypeMessageEntity] | None) -> list[LeadLink]:
    """Telegram links of a message in the order they appear: hidden links (`text_url`
    entities), visible ones (`url` entities) and tg:// or t.me links in plain text that
    carry no entity. Everything else (other sites, invites, broken links) is skipped."""
    text = text or ""
    encoded = text.encode("utf-16-le")
    found: list[tuple[int, str]] = []
    covered: list[tuple[int, int]] = []
    for entity in entities or ():
        if isinstance(entity, MessageEntityTextUrl):
            found.append((entity.offset, entity.url))
        elif isinstance(entity, MessageEntityUrl):
            found.append((entity.offset, _utf16_slice(encoded, entity.offset, entity.length)))
        else:
            continue
        covered.append((entity.offset, entity.offset + entity.length))
    for match in _TEXT_LINK.finditer(text):
        start = _utf16_len(text[: match.start()])
        if not any(lo <= start < hi for lo, hi in covered):
            found.append((start, match.group()))
    found.sort(key=lambda item: item[0])
    return [link for _, url in found if (link := parse_link(url)) is not None]


def pick_lead_link(links: Iterable[LeadLink]) -> LeadLink | None:
    """The lead's source: the last message link, else the last user link."""
    links = list(links)
    for kind in ("message", "user"):
        candidates = [link for link in links if link.kind == kind]
        if candidates:
            return candidates[-1]
    return None
