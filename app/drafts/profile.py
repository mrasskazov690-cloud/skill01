"""The service profile: the only facts a draft may state, and the reviews it may quote.

The real profile lives in `profile.local.yaml` (git-ignored, it holds real customer
reviews); `profile.example.yaml` in the repository shows the format with placeholders.
It is loaded and validated once at startup.
"""

# ruff: noqa: RUF001, RUF003  (Russian phrases and examples by design)

import re
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.logging import get_logger

# A disclosure phrase must name the tie itself: cooperation, work, representation,
# partnership. "советую сервис" / "рекомендую сервис" only advocate and disclose nothing.
_TIE = re.compile(
    r"(?<![а-яa-z])(?:сотруднича|сотрудничеств|сотрудник|работа[юе]|представля"
    r"|партн[её]р|агент)"
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    text: Text


class ServiceProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Text
    aliases: tuple[Text, ...] = ()
    countries: tuple[Text, ...] = Field(min_length=1)
    services: tuple[Text, ...] = Field(min_length=1)
    facts: tuple[Text, ...] = Field(min_length=1)
    reviews: tuple[Review, ...] = ()
    # Phrases that disclose the tie to the service ("сотрудничаю", "работаю с"): a
    # draft without one of them is never sent automatically. Only the phrases that
    # name the tie count (see `disclosures`); at least one must.
    disclosure_phrases: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_review_ids(self) -> "ServiceProfile":
        ids = [review.id for review in self.reviews]
        if len(ids) != len(set(ids)):
            raise ValueError("review ids must be unique")
        return self

    @model_validator(mode="after")
    def _discloses_the_tie(self) -> "ServiceProfile":
        if not self.disclosures:
            raise ValueError(
                "disclosure_phrases: none names the tie to the service"
                " (сотрудничаю, работаю с, представляю)"
            )
        return self

    @property
    def disclosures(self) -> tuple[str, ...]:
        """The disclosure phrases that name the tie; advocacy alone is not one."""
        return tuple(p for p in self.disclosure_phrases if _TIE.search(_fold(p)))

    @property
    def ignored_phrases(self) -> tuple[str, ...]:
        """Disclosure phrases that do not disclose anything and are not used."""
        return tuple(p for p in self.disclosure_phrases if p not in self.disclosures)

    @property
    def names(self) -> tuple[str, ...]:
        """The service's name and aliases, the name first, without repeats."""
        return tuple(dict.fromkeys((self.name, *self.aliases)))

    def review(self, review_id: str | None) -> Review | None:
        return next((r for r in self.reviews if r.id == review_id), None)


class ProfileError(ValueError):
    """The profile file is missing or invalid. The message names the problem, not the
    file's contents (it holds customer reviews)."""


def load_profile(path: str | Path) -> ServiceProfile:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileError(f"profile file not found: {path}") from exc
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ProfileError(f"profile file {path} cannot be read: {type(exc).__name__}") from exc
    if not isinstance(raw, dict):
        raise ProfileError(f"profile file {path} must hold a mapping")
    try:
        profile = ServiceProfile.model_validate(raw)
    except ValueError as exc:
        # Field locations and messages only: pydantic would echo the offending input.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in getattr(exc, "errors", lambda: [])()
        )
        raise ProfileError(f"profile file {path} is invalid: {problems or 'bad value'}") from None
    if profile.ignored_phrases:
        # The phrases are settings, not customer data.
        get_logger(__name__).warning(
            "profile_disclosure_phrases_ignored", phrases=list(profile.ignored_phrases)
        )
    return profile


def _fold(text: str) -> str:
    return text.lower().replace("ё", "е")
