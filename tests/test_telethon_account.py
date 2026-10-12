"""TelethonAccount against a substituted TelegramClient: no network, real Telethon types.

Errors are built the way Telethon builds them from a server answer
(rpc_message_to_error), so the mapping is tested against the real classes.
"""

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from telethon import errors
from telethon.crypto import AuthKey
from telethon.errors import rpc_message_to_error
from telethon.sessions import StringSession
from telethon.tl import types
from telethon.tl.functions.messages import SendMessageRequest, SendReactionRequest

from app.domain.states import AccountState
from app.telegram import telethon_account
from app.telegram.base import (
    AuthorUnavailable,
    FloodWait,
    PeerFlood,
    PeerInvalid,
    PeerMismatch,
    PeerRef,
    PermanentSendError,
    PrivacyRestricted,
    ResolvedAuthor,
    SendTimeout,
    SentMessage,
    SourceUnavailable,
    TelegramAccount,
    UserBlocked,
)
from app.telegram.links import LeadLink
from app.telegram.telethon_account import TEST_DC, TelethonAccount, map_error

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
AUTHOR = types.User(id=42, access_hash=4242, first_name="Иван", last_name="П", username="ivan")
SOURCE = LeadLink(kind="message", chat_username="arenda_msk", message_id=7, url="t.me/arenda_msk/7")
PRIVATE_SOURCE = LeadLink(
    kind="message", chat_internal_id=1234567890, message_id=8, url="t.me/c/1234567890/8"
)


def rpc(code: int, message: str, request: Any = None) -> errors.RPCError:
    return rpc_message_to_error(
        types.RpcError(error_code=code, error_message=message), request or SendMessageRequest
    )


def tg_message(
    message_id: int,
    *,
    sender: Any = AUTHOR,
    text: str = "",
    entities: list[Any] | None = None,
    reactions: list[str] | None = None,
    forwarded: bool = False,
) -> types.Message:
    message = types.Message(
        id=message_id,
        peer_id=types.PeerChannel(1),
        date=NOW,
        message=text,
        from_id=types.PeerUser(sender.id) if isinstance(sender, types.User) else None,
        entities=entities,
        fwd_from=types.MessageFwdHeader(date=NOW) if forwarded else None,
        reactions=types.MessageReactions(
            results=[
                types.ReactionCount(reaction=types.ReactionEmoji(emoticon=e), count=1)
                for e in reactions or ()
            ]
            + [types.ReactionCount(reaction=types.ReactionCustomEmoji(document_id=1), count=2)]
        )
        if reactions is not None
        else None,
    )
    message._sender = sender
    return message


class SubstitutedClient:
    """Stands in for TelegramClient: scripted answers, records every request."""

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.send_results: list[Any] = []  # results or exceptions for __call__
        self.messages: dict[tuple[Any, int], Any] = {}  # (chat, id) -> message | exception
        self.history: list[Any] = []  # what get_messages(limit=...) returns
        self.entities: dict[Any, Any] = {}  # username -> entity | exception
        self.input_entities: dict[Any, Any] = {}  # peer -> input peer (the session cache)
        self.after_dialogs: dict[Any, Any] = {}  # what get_dialogs() adds to the cache
        self.dialogs_loaded = 0
        self.own_messages: dict[int, list[Any]] = {}
        self.authorized = True
        self.me_error: BaseException | None = None

    async def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        result = self.send_results.pop(0) if self.send_results else None
        if isinstance(result, BaseException):
            raise result
        return result

    async def get_messages(self, entity: Any, limit: Any = None, **kwargs: Any) -> Any:
        if kwargs.get("ids") is not None:
            key = entity.channel_id if isinstance(entity, types.InputPeerChannel) else entity
            found = self.messages.get((key, kwargs["ids"]))
            if isinstance(found, BaseException):
                raise found
            return found
        if kwargs.get("from_user") == "me":
            return self.own_messages.get(entity.user_id, [])[:limit]
        newer = [m for m in self.history if m.id > kwargs.get("min_id", 0)]
        return sorted(newer, key=lambda m: m.id, reverse=True)[:limit]

    async def get_entity(self, entity: Any) -> Any:
        key = entity.user_id if isinstance(entity, types.PeerUser) else entity
        found = self.entities.get(key)
        if found is None:
            raise ValueError("Cannot find any entity corresponding to it")
        if isinstance(found, BaseException):
            raise found
        return found

    async def get_input_entity(self, peer: Any) -> Any:
        key = (
            peer.user_id
            if isinstance(peer, types.PeerUser)
            else peer.channel_id
            if isinstance(peer, types.PeerChannel)
            else peer
        )
        if key not in self.input_entities:
            raise ValueError("Could not find the input entity")
        return self.input_entities[key]

    async def get_dialogs(self) -> list[Any]:
        self.dialogs_loaded += 1
        self.input_entities.update(self.after_dialogs)
        return []

    def is_connected(self) -> bool:
        return True

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...

    async def is_user_authorized(self) -> bool:
        if self.me_error is not None:
            raise self.me_error
        return self.authorized

    async def get_me(self) -> Any:
        return AUTHOR


@pytest.fixture
def client() -> SubstitutedClient:
    client = SubstitutedClient()
    client.messages[("arenda_msk", 7)] = tg_message(7)
    return client


@pytest.fixture
def account(client: SubstitutedClient) -> TelethonAccount:
    return TelethonAccount(5, client)  # type: ignore[arg-type]


def sent(message_id: int = 900) -> types.UpdateShortSentMessage:
    return types.UpdateShortSentMessage(id=message_id, pts=1, pts_count=1, date=NOW)


async def test_implements_the_protocol(account: TelethonAccount) -> None:
    assert isinstance(account, TelegramAccount)
    assert account.account_id == 5


# --- send_text: whom it sends to ------------------------------------------------------


async def test_send_uses_the_sender_of_the_source_message(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    client.send_results = [sent(900)]
    result = await account.send_text(
        PeerRef(42, "ivan", SOURCE), "Здравствуйте", random_id=123456789
    )
    assert result == SentMessage(900)
    [request] = client.requests
    assert isinstance(request, SendMessageRequest)
    assert request.peer == types.InputPeerUser(user_id=42, access_hash=4242)
    assert (request.message, request.random_id, request.no_webpage) == (
        "Здравствуйте",
        123456789,
        True,
    )


async def test_send_reads_the_id_from_full_updates(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    client.send_results = [
        types.Updates(
            updates=[types.UpdateMessageID(id=901, random_id=1)],
            users=[],
            chats=[],
            date=NOW,
            seq=0,
        )
    ]
    assert await account.send_text(PeerRef(42, None, SOURCE), "x", random_id=1) == SentMessage(901)


async def test_private_source_is_found_after_loading_dialogs(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    channel = types.InputPeerChannel(channel_id=1234567890, access_hash=1)
    client.after_dialogs[1234567890] = channel
    client.messages[(1234567890, 8)] = tg_message(8)
    client.send_results = [sent()]
    await account.send_text(PeerRef(42, None, PRIVATE_SOURCE), "x", random_id=1)
    assert client.dialogs_loaded == 1
    assert client.requests[0].peer.user_id == 42


async def test_source_from_another_author_is_a_mismatch(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    other = types.User(id=43, access_hash=1, first_name="Другой")
    client.messages[("arenda_msk", 7)] = tg_message(7, sender=other)
    with pytest.raises(PeerMismatch):
        await account.send_text(PeerRef(42, "ivan", SOURCE), "x", random_id=1)
    assert client.requests == []


async def test_unreadable_source_falls_back_to_a_matching_username(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    client.messages[("arenda_msk", 7)] = rpc(400, "CHANNEL_PRIVATE")
    client.entities["ivan"] = AUTHOR
    client.send_results = [sent()]
    await account.send_text(PeerRef(42, "ivan", SOURCE), "x", random_id=1)
    assert client.requests[0].peer == types.InputPeerUser(user_id=42, access_hash=4242)


async def test_deleted_source_falls_back_to_the_username(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    client.messages[("arenda_msk", 7)] = None
    client.entities["ivan"] = AUTHOR
    client.send_results = [sent()]
    await account.send_text(PeerRef(42, "ivan", SOURCE), "x", random_id=1)
    assert len(client.requests) == 1


async def test_username_taken_by_someone_else_is_a_mismatch(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    client.entities["ivan"] = types.User(id=999, access_hash=1, username="ivan")
    with pytest.raises(PeerMismatch) as exc:
        await account.send_text(PeerRef(42, "ivan"), "x", random_id=1)
    assert isinstance(exc.value, PermanentSendError)
    assert client.requests == []


async def test_username_of_a_channel_is_a_mismatch(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    client.entities["ivan"] = types.Channel(
        id=42, title="c", photo=types.ChatPhotoEmpty(), date=NOW, access_hash=1
    )
    with pytest.raises(PeerMismatch):
        await account.send_text(PeerRef(42, "ivan"), "x", random_id=1)


@pytest.mark.parametrize(
    "peer",
    [
        PeerRef(42),  # no source, no username
        PeerRef(None, "ivan"),  # no id to check the username against
        PeerRef(42, "gone"),  # username not found
    ],
)
async def test_no_safe_way_to_the_author_is_peer_invalid(
    account: TelethonAccount, client: SubstitutedClient, peer: PeerRef
) -> None:
    client.entities["ivan"] = AUTHOR
    client.entities["gone"] = rpc(400, "USERNAME_NOT_OCCUPIED")
    with pytest.raises(PeerInvalid):
        await account.send_text(peer, "x", random_id=1)
    assert client.requests == []


# --- send_text: error mapping ---------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (rpc(420, "FLOOD_WAIT_37"), FloodWait),
        (rpc(420, "FLOOD_PREMIUM_WAIT_5"), FloodWait),
        (rpc(400, "PEER_FLOOD"), PeerFlood),
        (rpc(403, "USER_PRIVACY_RESTRICTED"), PrivacyRestricted),
        (rpc(403, "PRIVACY_PREMIUM_REQUIRED"), PrivacyRestricted),
        (rpc(400, "USER_IS_BLOCKED"), UserBlocked),
        (rpc(400, "INPUT_USER_DEACTIVATED"), PeerInvalid),
        (rpc(400, "PEER_ID_INVALID"), PeerInvalid),
        (rpc(500, "INTERNAL_SERVER_ERROR"), SendTimeout),
        (rpc(-503, "Timeout"), SendTimeout),
        (TimeoutError(), SendTimeout),
        (ConnectionError("Cannot send requests while disconnected"), SendTimeout),
    ],
)
async def test_send_error_mapping(
    account: TelethonAccount,
    client: SubstitutedClient,
    error: BaseException,
    expected: type[Exception],
) -> None:
    client.send_results = [error]
    with pytest.raises(expected) as exc:
        await account.send_text(PeerRef(42, None, SOURCE), "секретный текст", random_id=1)
    assert exc.value.__cause__ is error
    assert "секретный" not in str(exc.value)
    if expected is FloodWait:
        assert exc.value.seconds == error.seconds  # type: ignore[attr-defined]
    if expected in (PrivacyRestricted, UserBlocked, PeerInvalid):
        assert isinstance(exc.value, PermanentSendError)


def test_privacy_premium_required_is_a_plain_rpc_error() -> None:
    # Telethon 1.45 has no class for it: the mapping must go by the RPC message.
    error = rpc(403, "PRIVACY_PREMIUM_REQUIRED")
    assert type(error) is errors.ForbiddenError
    assert isinstance(map_error(error), PrivacyRestricted)
    assert map_error(rpc(403, "CHAT_WRITE_FORBIDDEN")) is None


async def test_random_id_duplicate_is_a_delivered_duplicate(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    error = rpc(500, "RANDOM_ID_DUPLICATE")
    assert isinstance(error, errors.RandomIdDuplicateError)
    assert isinstance(error, errors.ServerError)  # must not be mistaken for a timeout
    client.send_results = [error]
    result = await account.send_text(PeerRef(42, None, SOURCE), "x", random_id=77)
    assert result == SentMessage(None, duplicate=True)


async def test_unmapped_rpc_error_propagates_unchanged(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    error = rpc(403, "CHAT_WRITE_FORBIDDEN")
    client.send_results = [error]
    with pytest.raises(errors.ChatWriteForbiddenError) as exc:
        await account.send_text(PeerRef(42, None, SOURCE), "x", random_id=1)
    assert exc.value is error


async def test_flood_wait_while_opening_the_source(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    client.messages[("arenda_msk", 7)] = rpc(420, "FLOOD_WAIT_12", SendMessageRequest)
    with pytest.raises(FloodWait) as exc:
        await account.send_text(PeerRef(42, None, SOURCE), "x", random_id=1)
    assert exc.value.seconds == 12
    assert client.requests == []


# --- reading --------------------------------------------------------------------------


async def test_resolve_author_from_a_message_link(account: TelethonAccount) -> None:
    assert await account.resolve_author(SOURCE) == ResolvedAuthor(42, "ivan", "Иван П", False)


@pytest.mark.parametrize(
    ("sender", "expected"),
    [
        (
            types.Channel(id=1, title="c", photo=types.ChatPhotoEmpty(), date=NOW),
            AuthorUnavailable,
        ),
        (None, AuthorUnavailable),
        (types.User(id=50, deleted=True), AuthorUnavailable),
        (types.User(id=51, access_hash=1, bot=True, username="some_bot"), AuthorUnavailable),
    ],
)
async def test_resolve_author_without_a_user(
    account: TelethonAccount, client: SubstitutedClient, sender: Any, expected: type
) -> None:
    message = tg_message(7, sender=AUTHOR)
    message._sender = sender
    message._sender_id = None if sender is None else sender.id
    client.messages[("arenda_msk", 7)] = message
    with pytest.raises(expected):
        await account.resolve_author(SOURCE)


@pytest.mark.parametrize("outcome", [rpc(400, "CHANNEL_PRIVATE"), rpc(400, "MSG_ID_INVALID"), None])
async def test_resolve_author_with_an_unreadable_source(
    account: TelethonAccount, client: SubstitutedClient, outcome: Any
) -> None:
    client.messages[("arenda_msk", 7)] = outcome
    with pytest.raises(SourceUnavailable):
        await account.resolve_author(SOURCE)


async def test_resolve_author_private_chat_not_joined(account: TelethonAccount) -> None:
    with pytest.raises(SourceUnavailable):
        await account.resolve_author(PRIVATE_SOURCE)


async def test_resolve_author_from_user_links(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    client.entities["ivan"] = AUTHOR
    by_name = LeadLink(kind="user", username="ivan", url="t.me/ivan")
    assert (await account.resolve_author(by_name)).tg_user_id == 42
    by_id = LeadLink(kind="user", user_id=77, url="tg://user?id=77")
    assert await account.resolve_author(by_id) == ResolvedAuthor(77, None, None)
    client.entities["nobody"] = rpc(400, "USERNAME_NOT_OCCUPIED")
    with pytest.raises(SourceUnavailable):
        await account.resolve_author(LeadLink(kind="user", username="nobody", url="t.me/nobody"))


async def test_resolve_author_flood_wait(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    client.messages[("arenda_msk", 7)] = rpc(420, "FLOOD_WAIT_30")
    with pytest.raises(FloodWait):
        await account.resolve_author(SOURCE)


async def test_fetch_work_chat(account: TelethonAccount, client: SubstitutedClient) -> None:
    client.input_entities[-100500] = types.InputPeerChannel(channel_id=500, access_hash=1)
    text = "🔥 Заявка подробнее"
    hidden = types.MessageEntityTextUrl(offset=10, length=9, url="https://t.me/arenda_msk/7")
    client.history = [
        tg_message(10, text="старое"),
        tg_message(11, text=text, entities=[hidden], reactions=["❤"], forwarded=True),
        types.MessageService(
            id=12, peer_id=types.PeerChannel(500), date=NOW, action=types.MessageActionPinMessage()
        ),
        tg_message(13, text="без ссылки", reactions=["🚫"]),
    ]
    got = await account.fetch_work_chat(-100500, min_id=10, limit=50)
    assert [m.message_id for m in got] == [13, 11]
    newest, lead = got
    assert newest.reactions == {"🚫"} and newest.links == [] and not newest.forwarded
    assert lead.reactions == {"❤️"}  # Telegram's "❤" is reported in the worker's spelling
    assert lead.forwarded and lead.text == text and lead.chat_id == -100500
    assert [(link.chat_username, link.message_id) for link in lead.links] == [("arenda_msk", 7)]


async def test_fetch_work_chat_survives_malformed_links(
    account: TelethonAccount, client: SubstitutedClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    client.input_entities[-100500] = types.InputPeerChannel(channel_id=500, access_hash=1)
    client.history = [tg_message(1, text="автор tg://[x"), tg_message(2, text="t.me/arenda_msk/5")]
    got = await account.fetch_work_chat(-100500, limit=50)
    assert [(m.message_id, len(m.links)) for m in got] == [(2, 1), (1, 0)]

    def broken(text: str, entities: Any) -> list[LeadLink]:
        raise ValueError("unexpected")

    # Whatever the parser misses, one message never fails the whole read.
    monkeypatch.setattr(telethon_account, "extract_links", broken)
    got = await account.fetch_work_chat(-100500, limit=50)
    assert [(m.message_id, m.links) for m in got] == [(2, []), (1, [])]


async def test_fetch_work_chat_not_a_member(account: TelethonAccount) -> None:
    with pytest.raises(SourceUnavailable):
        await account.fetch_work_chat(-100501, limit=10)


async def test_has_dialog_with(account: TelethonAccount, client: SubstitutedClient) -> None:
    assert not await account.has_dialog_with(42)  # unknown even after loading dialogs
    peer = types.InputPeerUser(user_id=42, access_hash=4242)
    client.input_entities[42] = peer
    assert not await account.has_dialog_with(42)  # a dialog, but only their messages
    client.own_messages[42] = [tg_message(1)]
    assert await account.has_dialog_with(42)


# --- reactions and health -------------------------------------------------------------


async def test_react(account: TelethonAccount, client: SubstitutedClient) -> None:
    chat = types.InputPeerChannel(channel_id=500, access_hash=1)
    client.input_entities[-100500] = chat
    await account.react(-100500, 11, "❤️")
    await account.react(-100500, 12, "🚫")
    first, second = client.requests
    assert isinstance(first, SendReactionRequest)
    assert (first.peer, first.msg_id) == (chat, 11)
    assert first.reaction == [types.ReactionEmoji(emoticon="❤")]
    assert second.reaction == [types.ReactionEmoji(emoticon="🚫")]
    client.send_results = [rpc(420, "FLOOD_WAIT_3")]
    with pytest.raises(FloodWait):
        await account.react(-100500, 13, "❤️")


@pytest.mark.parametrize(
    ("authorized", "error", "state"),
    [
        (True, None, AccountState.ACTIVE),
        (False, None, AccountState.DISCONNECTED),
        (True, rpc(401, "AUTH_KEY_UNREGISTERED"), AccountState.DISCONNECTED),
        (True, rpc(401, "SESSION_REVOKED"), AccountState.DISCONNECTED),
        (True, rpc(401, "USER_DEACTIVATED_BAN"), AccountState.RESTRICTED),
        (True, ConnectionError(), AccountState.DISCONNECTED),
    ],
)
async def test_check_connection(
    account: TelethonAccount,
    client: SubstitutedClient,
    authorized: bool,
    error: BaseException | None,
    state: AccountState,
) -> None:
    client.authorized, client.me_error = authorized, error
    assert (await account.check_connection()).state is state


# --- sessions -------------------------------------------------------------------------


def test_session_comes_only_from_a_session_variable() -> None:
    with pytest.raises(ValueError, match="TG_SESSION_") as exc:
        TelethonAccount.from_env(
            1, "DATABASE_URL", api_id=1, api_hash="h", environ={"DATABASE_URL": "postgres://s"}
        )
    assert "postgres" not in str(exc.value)
    with pytest.raises(ValueError, match="TG_SESSION_MAIN is not set"):
        TelethonAccount.from_env(1, "TG_SESSION_MAIN", api_id=1, api_hash="h", environ={})
    with pytest.raises(ValueError) as exc:
        TelethonAccount.from_env(
            1, "TG_SESSION_MAIN", api_id=1, api_hash="h", environ={"TG_SESSION_MAIN": "garbage"}
        )
    assert "garbage" not in str(exc.value)


def _session_value() -> str:
    session = StringSession()
    session.set_dc(*TEST_DC)
    session.auth_key = AuthKey(bytes(range(256)))
    return session.save()


def test_session_from_dotenv_like_the_other_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """README: the session may be put into .env, which Settings reads but never exports."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TG_SESSION_ANNA", raising=False)
    (tmp_path / ".env").write_text(f"TG_SESSION_ANNA={_session_value()}\n")
    account = TelethonAccount.from_env(1, "TG_SESSION_ANNA", api_id=1, api_hash="h")
    assert account.client.session.auth_key.key == bytes(range(256))
    assert "TG_SESSION_ANNA" not in os.environ
    monkeypatch.setenv("TG_SESSION_ANNA", "garbage")  # the environment comes first
    with pytest.raises(ValueError):
        TelethonAccount.from_env(1, "TG_SESSION_ANNA", api_id=1, api_hash="h")
    monkeypatch.delenv("TG_SESSION_ANNA")
    (tmp_path / ".env").write_text("TG_SESSION_BOB=x\n")
    with pytest.raises(ValueError, match="TG_SESSION_ANNA is not set"):
        TelethonAccount.from_env(1, "TG_SESSION_ANNA", api_id=1, api_hash="h")


def test_client_from_env_never_sleeps_on_flood_and_does_not_retry() -> None:
    value = _session_value()
    account = TelethonAccount.from_env(
        3, "TG_TEST_SESSION_A", api_id=1, api_hash="h", environ={"TG_TEST_SESSION_A": value}
    )
    client = account.client
    assert client.session.auth_key.key == bytes(range(256))
    assert client.flood_sleep_threshold == 0
    assert client._request_retries == 1 and client._raise_last_call_error


async def test_flood_wait_while_loading_dialogs_is_mapped(
    account: TelethonAccount, client: SubstitutedClient
) -> None:
    async def flooded() -> list[Any]:
        raise rpc(420, "FLOOD_WAIT_9")

    client.get_dialogs = flooded  # type: ignore[method-assign]
    with pytest.raises(FloodWait):
        await account.has_dialog_with(42)
