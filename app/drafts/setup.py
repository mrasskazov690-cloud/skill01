"""DRAFT_PROVIDER: pick the draft provider at startup and load the service profile."""

from pathlib import Path

import anthropic

from app.config import Settings
from app.drafts.llm import ClaudeDraftProvider, MessagesClient
from app.drafts.profile import ServiceProfile, load_profile
from app.worker.drafts import DraftProvider, TemplateDraftProvider


class DraftSetupError(ValueError):
    """A setting the chosen provider needs is missing or wrong (names it, no values)."""


def make_draft_provider(
    settings: Settings, client: MessagesClient | None = None
) -> tuple[DraftProvider, ServiceProfile | None]:
    """The provider and the profile the guard checks drafts against.

    llm: the profile (PROFILE_PATH) and ANTHROPIC_API_KEY are required. template: the
    profile is loaded when the file exists (so drafts typed by hand are checked
    against it too) and is optional otherwise.
    """
    if settings.DRAFT_PROVIDER == "template":
        profile = (
            load_profile(settings.PROFILE_PATH) if Path(settings.PROFILE_PATH).is_file() else None
        )
        return TemplateDraftProvider(), profile
    profile = load_profile(settings.PROFILE_PATH)
    if client is None:
        if settings.ANTHROPIC_API_KEY is None:
            raise DraftSetupError("DRAFT_PROVIDER=llm needs ANTHROPIC_API_KEY")
        client = anthropic.AsyncAnthropic(
            api_key=settings.ANTHROPIC_API_KEY.get_secret_value(), timeout=120.0
        )
    return ClaudeDraftProvider(client, profile), profile
