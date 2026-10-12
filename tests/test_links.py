import pytest
from telethon.tl.types import MessageEntityBold, MessageEntityTextUrl, MessageEntityUrl

from app.telegram.links import LeadLink, extract_links, parse_link, pick_lead_link


def msg(chat: str | None = None, message_id: int = 0, internal: int | None = None) -> dict:
    return {
        "kind": "message",
        "chat_username": chat,
        "chat_internal_id": internal,
        "message_id": message_id,
    }


def user(username: str | None = None, user_id: int | None = None) -> dict:
    return {"kind": "user", "username": username, "user_id": user_id}


PARSED = [
    # t.me/<user>, with every scheme and host form
    ("t.me/ivan_petrov", user("ivan_petrov")),
    ("http://t.me/ivan_petrov", user("ivan_petrov")),
    ("https://t.me/ivan_petrov", user("ivan_petrov")),
    ("https://telegram.me/ivan_petrov", user("ivan_petrov")),
    ("telegram.me/ivan_petrov", user("ivan_petrov")),
    ("HTTPS://T.ME/Ivan_Petrov", user("Ivan_Petrov")),
    ("https://www.t.me/ivan_petrov", user("ivan_petrov")),
    ("https://t.me/ivan_petrov?start=abc", user("ivan_petrov")),
    ("https://t.me/@ivan_petrov", user("ivan_petrov")),
    # t.me/<group>/<msg>
    ("t.me/arenda_msk/12345", msg("arenda_msk", 12345)),
    ("http://t.me/arenda_msk/12345", msg("arenda_msk", 12345)),
    ("https://t.me/arenda_msk/12345", msg("arenda_msk", 12345)),
    ("https://telegram.me/arenda_msk/12345", msg("arenda_msk", 12345)),
    ("https://t.me/arenda_msk/12345?single", msg("arenda_msk", 12345)),
    ("https://t.me/arenda_msk/12345/", msg("arenda_msk", 12345)),
    ("https://t.me/s/arenda_msk/12345", msg("arenda_msk", 12345)),
    # t.me/<group>/<topic>/<msg>
    ("https://t.me/arenda_msk/7/12345", msg("arenda_msk", 12345)),
    ("t.me/arenda_msk/7/12345", msg("arenda_msk", 12345)),
    # t.me/c/<id>/<msg> and its topic form
    ("https://t.me/c/1234567890/55", msg(None, 55, 1234567890)),
    ("t.me/c/1234567890/55", msg(None, 55, 1234567890)),
    ("http://telegram.me/c/1234567890/55", msg(None, 55, 1234567890)),
    ("https://t.me/c/1234567890/3/55", msg(None, 55, 1234567890)),
    # tg:// links
    ("tg://user?id=123456789", user(user_id=123456789)),
    ("tg://resolve?domain=ivan_petrov", user("ivan_petrov")),
    ("tg://resolve?domain=arenda_msk&post=12345", msg("arenda_msk", 12345)),
    ("tg://resolve?post=12345&domain=arenda_msk", msg("arenda_msk", 12345)),
    ("tg://privatepost?channel=1234567890&post=55", msg(None, 55, 1234567890)),
    ("TG://privatepost?channel=1234567890&post=55&thread=3", msg(None, 55, 1234567890)),
]


@pytest.mark.parametrize(("url", "expected"), PARSED)
def test_parse_link(url: str, expected: dict) -> None:
    link = parse_link(url)
    assert link is not None
    fields = {
        "chat_username": None,
        "chat_internal_id": None,
        "message_id": None,
        "username": None,
        "user_id": None,
        **expected,
    }
    assert {k: getattr(link, k) for k in fields} == fields
    assert link.url == url


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/arenda_msk/1",
        "https://t.me/+AbCdEfGh123",
        "https://t.me/joinchat/AbCdEf",
        "https://t.me/addstickers/pack",
        "https://t.me/share/url?url=x",
        "https://t.me/c/abc/1",
        "https://t.me/c/123",
        "https://t.me/arenda_msk/not_a_number",
        "https://t.me/ab/1",  # too short for a username
        "https://t.me/",
        "https://t.me.evil.com/arenda_msk/1",
        "ftp://t.me/arenda_msk/1",
        "tg://user?id=abc",
        "tg://resolve?domain=x",
        "tg://privatepost?channel=1",
        "tg://msg?text=hi",
        "",
        # Malformed or out of Telegram's ranges: rejected, never raised (one such message
        # in the work chat used to stop intake for good).
        "https://[t.me/durov",
        "tg://[oops",
        "tg://user\u2100?id=1",
        "https://t.me/arenda_msk/2147483648",  # message ids are int32
        "https://t.me/c/123/99999999999",
        "tg://resolve?domain=arenda_msk&post=99999999999",
        "tg://privatepost?channel=1&post=99999999999",
        "tg://user?id=9999999999999999999",  # user ids are int64
        "https://t.me/c/9999999999999999999/1",
    ],
)
def test_not_a_lead_link(url: str) -> None:
    assert parse_link(url) is None


def test_largest_ids_telegram_allows_are_kept() -> None:
    assert parse_link("https://t.me/arenda_msk/2147483647").message_id == 2**31 - 1  # type: ignore[union-attr]
    assert parse_link("tg://user?id=9223372036854775807").user_id == 2**63 - 1  # type: ignore[union-attr]


def test_malformed_plain_text_links_are_skipped() -> None:
    text = "автор tg://[x, ещё tg://user\u2100?id=1, заявка t.me/arenda_msk/5"
    assert [link.message_id for link in extract_links(text, [])] == [5]


def test_hidden_link_entity() -> None:
    text = "Новая заявка, подробности"
    entity = MessageEntityTextUrl(offset=14, length=11, url="https://t.me/arenda_msk/12345")
    [link] = extract_links(text, [entity])
    assert (link.kind, link.chat_username, link.message_id) == ("message", "arenda_msk", 12345)
    assert link.url == "https://t.me/arenda_msk/12345"


def test_url_entity_offsets_are_utf16() -> None:
    # Each emoji outside the BMP takes two UTF-16 units; a naive str slice would be off.
    text = "🔥🏠 Заявка: t.me/arenda_msk/77 срочно"
    prefix = "🔥🏠 Заявка: "
    offset = len(prefix.encode("utf-16-le")) // 2
    assert offset == len(prefix) + 2
    entity = MessageEntityUrl(offset=offset, length=len("t.me/arenda_msk/77"))
    [link] = extract_links(text, [entity])
    assert (link.chat_username, link.message_id, link.url) == (
        "arenda_msk",
        77,
        "t.me/arenda_msk/77",
    )


def test_order_of_hidden_visible_and_plain_links_and_the_pick() -> None:
    text = "👤 автор tg://user?id=42, источник тут и ещё t.me/other_group/5"
    hidden_at = len("👤 автор tg://user?id=42, источник ".encode("utf-16-le")) // 2
    visible_at = len("👤 автор tg://user?id=42, источник тут и ещё ".encode("utf-16-le")) // 2
    entities = [
        MessageEntityBold(offset=0, length=2),
        MessageEntityUrl(offset=visible_at, length=len("t.me/other_group/5")),
        MessageEntityTextUrl(offset=hidden_at, length=3, url="https://t.me/c/1234567890/9"),
    ]
    links = extract_links(text, entities)
    assert [(link.kind, link.message_id, link.user_id) for link in links] == [
        ("user", None, 42),  # tg:// in plain text: Telegram gives it no entity
        ("message", 9, None),
        ("message", 5, None),
    ]
    picked = pick_lead_link(links)
    assert picked is not None and picked.chat_username == "other_group"


def test_plain_text_links_without_entities() -> None:
    text = "см. https://t.me/arenda_msk/1, еще tg://privatepost?channel=99&post=3."
    links = extract_links(text, None)
    assert [(link.chat_username, link.chat_internal_id, link.message_id) for link in links] == [
        ("arenda_msk", None, 1),
        (None, 99, 3),
    ]
    assert links[1].url == "tg://privatepost?channel=99&post=3"
    assert extract_links("not.t.me/arenda_msk/1 и example.com", None) == []
    assert extract_links(None, None) == []


def test_url_entity_is_not_counted_twice() -> None:
    text = "заявка t.me/arenda_msk/1"
    entity = MessageEntityUrl(offset=7, length=17)
    assert len(extract_links(text, [entity])) == 1


def test_pick_prefers_message_links_then_the_last_one() -> None:
    first = LeadLink(kind="message", chat_username="a_group", message_id=1, url="u1")
    profile = LeadLink(kind="user", username="someone", url="u2")
    last = LeadLink(kind="message", chat_internal_id=5, message_id=2, url="u3")
    assert pick_lead_link([first, profile, last]) == last
    assert pick_lead_link([first, profile]) == first
    other = LeadLink(kind="user", user_id=9, url="u4")
    assert pick_lead_link([profile, other]) == other
    assert pick_lead_link([]) is None
