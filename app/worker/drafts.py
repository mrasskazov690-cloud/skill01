"""Where reply drafts come from. Templates now; an LLM provider plugs in later."""

from dataclasses import dataclass
from typing import Protocol

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


class DraftProvider(Protocol):
    async def draft(self, session: AsyncSession, message_id: int) -> Reply | None:
        """A reply for the lead, or None when there is nothing to offer."""
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
