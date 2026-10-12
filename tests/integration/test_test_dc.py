"""End-to-end check of TelethonAccount on Telegram's test servers (never production).

Runs only with TG_API_ID, TG_API_HASH, TG_TEST_SESSION_A and TG_TEST_SESSION_B set:
two test-DC accounts logged in with `python -m app.telegram.login --test-dc`.
Scenario: A creates a source group and a work chat, B joins the source group and
posts a lead, A posts it to the work chat behind a hidden link; A reads the work
chat, resolves the author by the link, replies to B by the source message, repeats
the send with the same random_id (hypothesis H1) and puts ❤️ on the work-chat post.
"""

import os
import secrets

import pytest
from telethon.tl.functions.channels import CreateChannelRequest, DeleteChannelRequest
from telethon.tl.functions.messages import ExportChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import MessageEntityTextUrl

from app.telegram.base import PeerRef
from app.telegram.links import pick_lead_link
from app.telegram.telethon_account import TelethonAccount

REQUIRED = ("TG_API_ID", "TG_API_HASH", "TG_TEST_SESSION_A", "TG_TEST_SESSION_B")

pytestmark = [
    pytest.mark.telegram_test_dc,
    pytest.mark.skipif(
        not all(os.environ.get(name) for name in REQUIRED),
        reason="Telegram test DC: set " + ", ".join(REQUIRED),
    ),
]


def _account(account_id: int, session_env: str) -> TelethonAccount:
    return TelethonAccount.from_env(
        account_id,
        session_env,
        api_id=int(os.environ["TG_API_ID"]),
        api_hash=os.environ["TG_API_HASH"],
    )


async def test_lead_reply_reaction_and_random_id_repeat() -> None:
    async with _account(1, "TG_TEST_SESSION_A") as a, _account(2, "TG_TEST_SESSION_B") as b:
        source = (
            await a.client(
                CreateChannelRequest(title="dispatcher test source", about="", megagroup=True)
            )
        ).chats[0]
        work = (
            await a.client(
                CreateChannelRequest(title="dispatcher test work", about="", megagroup=True)
            )
        ).chats[0]
        try:
            invite = await a.client(ExportChatInviteRequest(peer=source))
            await b.client(ImportChatInviteRequest(invite.link.rsplit("+", 1)[-1]))
            me_b = await b.client.get_me()
            lead = await b.client.send_message(source.id, "Ищу квартиру, тестовая заявка")

            label = "подробнее"
            url = f"https://t.me/c/{source.id}/{lead.id}"
            text = f"Новая заявка {label}"
            post = await a.client.send_message(
                work,
                text,
                formatting_entities=[MessageEntityTextUrl(offset=13, length=len(label), url=url)],
            )
            work_chat_id = await a.client.get_peer_id(work)

            [fetched] = [
                m
                for m in await a.fetch_work_chat(work_chat_id, limit=10)
                if m.message_id == post.id
            ]
            link = pick_lead_link(fetched.links)
            assert link is not None and link.chat_internal_id == source.id
            author = await a.resolve_author(link)
            assert author.tg_user_id == me_b.id

            assert not await a.has_dialog_with(me_b.id)
            random_id = secrets.randbits(63)
            peer = PeerRef(author.tg_user_id, author.username, link)
            first = await a.send_text(peer, "Здравствуйте! Тестовый ответ.", random_id=random_id)
            assert first.tg_message_id is not None and not first.duplicate
            # H1: a repeat with the same random_id must not deliver a second copy.
            repeat = await a.send_text(peer, "Здравствуйте! Тестовый ответ.", random_id=random_id)
            await b.client.get_dialogs()  # updates are off: fill B's cache with A's entity
            inbox = await b.client.get_messages((await a.client.get_me()).id, limit=5)
            assert [m.message for m in inbox].count("Здравствуйте! Тестовый ответ.") == 1
            assert repeat.duplicate or repeat.tg_message_id == first.tg_message_id
            assert await a.has_dialog_with(me_b.id)

            await a.react(work_chat_id, post.id, "❤️")
            [after] = [
                m
                for m in await a.fetch_work_chat(work_chat_id, limit=10)
                if m.message_id == post.id
            ]
            assert "❤️" in after.reactions
        finally:
            await a.client(DeleteChannelRequest(source))
            await a.client(DeleteChannelRequest(work))
