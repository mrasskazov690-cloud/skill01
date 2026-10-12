"""Where reply drafts come from: the active template, or Claude (app.drafts.llm).

A provider answers with a Reply, with NoDraft (the lead goes to the owner with that
review reason) or None (no template: 'no_template'). It raises DraftUnavailable for a
passing failure (rate limit, server error, network): the lead stays in the queue and
is tried again on the next tick.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Template
from app.domain.states import DraftSource

DEFAULT_INTENT = "default"


@dataclass(frozen=True, slots=True)
class Reply:
    body: str
    source: DraftSource
    template_id: int | None = None
    template_version: int | None = None
    # What the provider made of the lead (country, need...), kept in messages.meta
    # under "draft_info" for the owner's card. Never logged.
    info: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NoDraft:
    """No reply for this lead: it goes to the owner as awaiting_review(reason)."""

    reason: str
    error: str | None = None  # an error code for the audit log, never text
    info: Mapping[str, Any] = field(default_factory=dict)


class DraftUnavailable(Exception):
    """The provider cannot answer right now; try the lead again later."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class DraftProvider(Protocol):
    async def draft(self, session: AsyncSession, message_id: int) -> Reply | NoDraft | None:
        """A reply for the lead, NoDraft, or None when there is no template."""
        ...


class TemplateDraftProvider:
    """The active template for an intent (the newest version wins)."""

    def __init__(self, intent: str = DEFAULT_INTENT) -> None:
        self.intent = intent

    async def draft(self, session: AsyncSession, message_id: int) -> Reply | None:
        template = (
            await session.execute(
                select(Template.id, Template.version, Template.body)
                .where(Template.active.is_(True), Template.intent == self.intent)
                .order_by(Template.version.desc(), Template.id.desc())
                .limit(1)
            )
        ).one_or_none()
        if template is None:
            return None
        return Reply(template.body, DraftSource.TEMPLATE, template.id, template.version)
