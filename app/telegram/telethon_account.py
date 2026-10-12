"""TelegramAccount on Telethon (MTProto, a user account).

The session is a StringSession read from the environment variable named in
accounts.session_env; it never reaches logs, exceptions or the database. Every
Telethon error a caller acts on is mapped to the exceptions of app.telegram.base;
anything else propagates unchanged (outbox treats it as an uncertain delivery).

A user's access_hash is valid only for the account that obtained it, so the sending
account opens the lead's source message itself; a username is used only if it resolves
to the client's tg_user_id right now.
"""

import re
from types import TracebackType
from typing import Any, NoReturn, Self

from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.messages import SendMessageRequest, SendReactionRequest
from telethon.tl.types import (
    Channel,
    Chat,
    InputPeerUser,
    PeerChannel,
    PeerUser,
    ReactionEmoji,
    UpdateMessageID,
    Updates,
    UpdatesCombined,
    UpdateShortSentMessage,
    User,
)
from telethon.tl.types import Message as TlMessage

from app.config import read_env
from app.domain.states import AccountState
from app.telegram.base import (
    AccountHealth,
    AuthorUnavailable,
    FloodWait,
    PeerFlood,
    PeerInvalid,
    PeerMismatch,
    PeerRef,
    PrivacyRestricted,
    ReactionInvalid,
    ResolvedAuthor,
    SendRefused,
    SendTimeout,
    SentMessage,
    SourceUnavailable,
    UserBlocked,
    UserRestricted,
    WorkChatMessage,
)
from app.telegram.links import LeadLink, extract_links

# Only these variables may hold sessions: the name comes from the database, which must
# not be able to point the adapter at DATABASE_URL or any other secret.
SESSION_ENV = re.compile(r"TG_(?:TEST_)?SESSION_[A-Z0-9_]+")
TEST_DC = (2, "149.154.167.40", 80)
# Not in Telethon 1.45's error list; arrives as a plain RPCError (403).
PRIVACY_PREMIUM_REQUIRED = "PRIVACY_PREMIUM_REQUIRED"
# Answers that mean "this chat/message cannot be read by this account".
_SOURCE_ERRORS: tuple[type[BaseException], ...] = (
    errors.ChannelPrivateError,
    errors.ChannelInvalidError,
    errors.ChatAdminRequiredError,
    errors.MsgIdInvalidError,
    errors.UsernameNotOccupiedError,
    errors.UsernameInvalidError,
    errors.PeerIdInvalidError,
    ValueError,  # Telethon: entity not found in this session
)
_TIMEOUTS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    errors.ServerError,
    errors.TimedOutError,
)
# A send answered with one of these (and no mapping of its own) was refused by the
# server: nothing was delivered, so it is a definitive failure, not an uncertain one.
_REFUSALS: tuple[type[BaseException], ...] = (
    errors.BadRequestError,
    errors.UnauthorizedError,
    errors.ForbiddenError,
    errors.NotFoundError,
    errors.AuthKeyError,
)


def map_error(exc: BaseException) -> BaseException | None:
    """The base exception for a Telethon error, or None if it has no mapping."""
    match exc:
        case errors.FloodWaitError() | errors.FloodPremiumWaitError():
            return FloodWait(exc.seconds)
        case errors.PeerFloodError():
            return PeerFlood()
        case errors.UserPrivacyRestrictedError():
            return PrivacyRestricted()
        case errors.RPCError() if exc.message == PRIVACY_PREMIUM_REQUIRED:
            return PrivacyRestricted()
        case errors.UserIsBlockedError():
            return UserBlocked()
        case errors.UserRestrictedError():
            return UserRestricted()
        case errors.ReactionInvalidError():
            return ReactionInvalid()
        case errors.InputUserDeactivatedError() | errors.PeerIdInvalidError():
            return PeerInvalid()
    return None


def _reraise(exc: BaseException) -> NoReturn:
    mapped = map_error(exc)
    if mapped is None:
        raise exc
    raise mapped from exc


def _emoticon_to_api(emoji: str) -> str:
    # Telegram's reaction emoticons carry no variation selector ("❤", not "❤️").
    return emoji.replace("️", "")


def _emoticon_from_api(emoticon: str) -> str:
    return "❤️" if emoticon == "❤" else emoticon


class TelethonAccount:
    def __init__(self, account_id: int, client: TelegramClient) -> None:
        self._account_id = account_id
        self._client = client

    @classmethod
    def from_env(
        cls,
        account_id: int,
        session_env: str,
        *,
        api_id: int,
        api_hash: str,
        environ: dict[str, str] | None = None,
    ) -> Self:
        if not SESSION_ENV.fullmatch(session_env):
            raise ValueError(f"session_env must look like TG_SESSION_<NAME>, got {session_env}")
        session = read_env(session_env) if environ is None else environ.get(session_env)
        if not session:
            raise ValueError(f"{session_env} is not set")
        return cls(account_id, make_client(StringSession(session), api_id, api_hash))

    @property
    def account_id(self) -> int:
        return self._account_id

    @property
    def client(self) -> TelegramClient:
        return self._client

    async def __aenter__(self) -> Self:
        await self._client.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.disconnect()

    # --- reading -------------------------------------------------------------------

    async def fetch_work_chat(
        self, chat_id: int, *, min_id: int = 0, limit: int
    ) -> list[WorkChatMessage]:
        chat = await self._input_entity(chat_id)
        try:
            messages = await self._client.get_messages(chat, limit=limit, min_id=min_id)
        except _SOURCE_ERRORS as exc:
            raise SourceUnavailable(f"chat {chat_id}") from exc
        except errors.RPCError as exc:
            _reraise(exc)
        result = []
        for message in messages:
            if not isinstance(message, TlMessage):  # service messages, empty slots
                continue
            text = message.message or ""
            reactions = _reactions(message)
            result.append(
                WorkChatMessage(
                    chat_id=chat_id,
                    message_id=message.id,
                    date=message.date,
                    text=text,
                    links=_links(text, message.entities),
                    reactions=reactions,
                    forwarded=message.fwd_from is not None,
                )
            )
        result.sort(key=lambda m: m.message_id, reverse=True)
        return result

    async def resolve_author(self, link: LeadLink) -> ResolvedAuthor:
        try:
            if link.kind == "message":
                message = await self._open_source(link)
                sender = message.sender
                if sender is None and message.sender_id is not None and message.sender_id > 0:
                    sender = await message.get_sender()
                return _author(sender)
            if link.username is not None:
                try:
                    entity = await self._client.get_entity(link.username)
                except _SOURCE_ERRORS as exc:
                    raise SourceUnavailable("profile") from exc
                return _author(entity)
            if link.user_id is not None:
                try:
                    return _author(await self._client.get_entity(PeerUser(link.user_id)))
                except ValueError:
                    # Not cached for this account; tg://user?id= names the id directly.
                    # Its username is unknown then, which is not the same as none.
                    return ResolvedAuthor(link.user_id, None, None, username_known=False)
        except errors.RPCError as exc:
            _reraise(exc)
        raise SourceUnavailable("empty link")

    async def has_dialog_with(self, tg_user_id: int) -> bool:
        try:
            peer = await self._input_entity(PeerUser(tg_user_id))
        except SourceUnavailable:
            return False  # not even among this account's dialogs
        try:
            ours = await self._client.get_messages(peer, limit=1, from_user="me")
        except errors.RPCError as exc:
            _reraise(exc)
        return len(ours) > 0

    # --- writing -------------------------------------------------------------------

    async def send_text(self, peer: PeerRef, text: str, *, random_id: int) -> SentMessage:
        try:
            input_peer = await self._input_user(peer)
            result = await self._client(
                SendMessageRequest(
                    peer=input_peer, message=text, random_id=random_id, no_webpage=True
                )
            )
        except errors.RandomIdDuplicateError:
            # An earlier request with this random_id was delivered (H1).
            return SentMessage(None, duplicate=True)
        except _TIMEOUTS as exc:
            raise SendTimeout() from exc
        except errors.RPCError as exc:
            if map_error(exc) is None and isinstance(exc, _REFUSALS):
                raise SendRefused(exc.message) from exc
            _reraise(exc)
        return SentMessage(_sent_id(result))

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        chat = await self._input_entity(chat_id)
        try:
            await self._client(
                SendReactionRequest(
                    peer=chat,
                    msg_id=message_id,
                    reaction=[ReactionEmoji(emoticon=_emoticon_to_api(emoji))],
                )
            )
        except errors.RPCError as exc:
            _reraise(exc)

    async def fetch_reactions(self, chat_id: int, message_id: int) -> set[str]:
        chat = await self._input_entity(chat_id)
        try:
            message = await self._client.get_messages(chat, ids=message_id)
        except _SOURCE_ERRORS as exc:
            raise SourceUnavailable("message") from exc
        except errors.RPCError as exc:
            _reraise(exc)
        if not isinstance(message, TlMessage):
            raise SourceUnavailable("message deleted")
        return _reactions(message)

    async def check_connection(self) -> AccountHealth:
        try:
            if not self._client.is_connected():
                await self._client.connect()
            if not await self._client.is_user_authorized():
                return AccountHealth(AccountState.DISCONNECTED, "not_authorized")
            me = await self._client.get_me()
        except (errors.UserDeactivatedBanError, errors.UserDeactivatedError):
            return AccountHealth(AccountState.RESTRICTED, "deactivated")
        except (errors.AuthKeyUnregisteredError, errors.SessionRevokedError):
            return AccountHealth(AccountState.DISCONNECTED, "session_revoked")
        except (TimeoutError, ConnectionError, OSError):
            return AccountHealth(AccountState.DISCONNECTED, "connection_failed")
        except errors.FloodWaitError as exc:
            return AccountHealth(AccountState.FLOOD_WAIT, f"flood_wait {exc.seconds}s")
        if me is None:
            return AccountHealth(AccountState.DISCONNECTED, "not_authorized")
        return AccountHealth(AccountState.ACTIVE)

    # --- helpers -------------------------------------------------------------------

    async def _input_entity(self, peer: Any) -> Any:
        """The input peer of a chat or user known to this account. Peers by bare id are
        only found in the session cache, so the dialog list is loaded once on a miss."""
        try:
            try:
                return await self._client.get_input_entity(peer)
            except ValueError:
                await self._client.get_dialogs()
                return await self._client.get_input_entity(peer)
        except _SOURCE_ERRORS as exc:
            raise SourceUnavailable("peer") from exc
        except errors.RPCError as exc:
            _reraise(exc)

    async def _open_source(self, link: LeadLink) -> Any:
        if link.message_id is None:
            raise SourceUnavailable("link has no message")
        if link.chat_username is not None:
            chat: Any = link.chat_username
        elif link.chat_internal_id is not None:
            chat = await self._input_entity(PeerChannel(link.chat_internal_id))
        else:
            raise SourceUnavailable("link has no chat")
        try:
            message = await self._client.get_messages(chat, ids=link.message_id)
        except _SOURCE_ERRORS as exc:
            raise SourceUnavailable("message") from exc
        if not isinstance(message, TlMessage):
            raise SourceUnavailable("message deleted")
        return message

    async def _input_user(self, peer: PeerRef) -> InputPeerUser:
        if peer.tg_user_id is None:
            # Nothing to check a username against: it may belong to someone else now.
            raise PeerInvalid()
        source = peer.source
        if source is not None and source.kind == "message":
            try:
                message = await self._open_source(source)
            except SourceUnavailable:
                message = None
            if message is not None:
                sender = message.sender
                if sender is not None and getattr(sender, "id", None) != peer.tg_user_id:
                    raise PeerMismatch()
                found = _input_peer_user(sender)
                if found is not None:
                    return found
        username = peer.username or (source.username if source is not None else None)
        if username is not None:
            try:
                entity = await self._client.get_entity(username)
            except _SOURCE_ERRORS:
                entity = None
            if entity is not None:
                if not isinstance(entity, User) or entity.id != peer.tg_user_id:
                    raise PeerMismatch()
                found = _input_peer_user(entity)
                if found is not None:
                    return found
        raise PeerInvalid()


def make_client(session: StringSession, api_id: int, api_hash: str) -> TelegramClient:
    # FloodWait is the caller's to schedule (never slept on here); retries are off and
    # the last error is raised, so RANDOM_ID_DUPLICATE (a ServerError) is not swallowed.
    return TelegramClient(
        session,
        api_id,
        api_hash,
        flood_sleep_threshold=0,
        request_retries=1,
        raise_last_call_error=True,
        receive_updates=False,
    )


def _links(text: str, entities: Any) -> list[LeadLink]:
    # One odd message must not fail the whole read (and so stop intake): without
    # links it is stored for a person to look at.
    try:
        return extract_links(text, entities)
    except Exception:
        return []


def _reactions(message: TlMessage) -> set[str]:
    return {
        _emoticon_from_api(count.reaction.emoticon)
        for count in (message.reactions.results if message.reactions else ())
        if isinstance(count.reaction, ReactionEmoji) and count.count > 0
    }


def _input_peer_user(entity: Any) -> InputPeerUser | None:
    if (
        isinstance(entity, User)
        and not entity.min
        and not entity.deleted
        and entity.access_hash is not None
    ):
        return InputPeerUser(user_id=entity.id, access_hash=entity.access_hash)
    return None


def _author(entity: Any) -> ResolvedAuthor:
    if isinstance(entity, Channel | Chat) or entity is None:
        raise AuthorUnavailable("not a user")  # anonymous admin, channel post, group
    if not isinstance(entity, User):
        raise AuthorUnavailable("unknown sender")
    if entity.deleted:
        raise AuthorUnavailable("deleted account")
    if entity.bot:
        raise AuthorUnavailable("bot")
    name = " ".join(p for p in (entity.first_name, entity.last_name) if p) or None
    # A user may have only collectible usernames (`usernames`, `username` empty). A
    # "min" constructor may leave fields out, so its missing username proves nothing.
    username = entity.username or next(
        (u.username for u in entity.usernames or () if u.active), None
    )
    return ResolvedAuthor(
        entity.id, username, name, bool(entity.bot), username_known=not entity.min
    )


def _sent_id(result: Any) -> int | None:
    """The new message id from SendMessageRequest's answer."""
    if isinstance(result, UpdateShortSentMessage):
        return result.id
    if isinstance(result, Updates | UpdatesCombined):
        for update in result.updates:
            if isinstance(update, UpdateMessageID):
                return update.id
    return None
