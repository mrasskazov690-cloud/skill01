"""The service profile: the only facts a draft may state, and the reviews it may quote.

The real profile lives in `profile.local.yaml` (git-ignored, it holds real customer
reviews); `profile.example.yaml` in the repository shows the format with placeholders.
It is loaded and validated once at startup.
"""

# ruff: noqa: RUF003  (Russian examples in comments)

from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

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
    # draft without one of them is never sent automatically.
    disclosure_phrases: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_review_ids(self) -> "ServiceProfile":
        ids = [review.id for review in self.reviews]
        if len(ids) != len(set(ids)):
            raise ValueError("review ids must be unique")
        return self

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
        return ServiceProfile.model_validate(raw)
    except ValueError as exc:
        # Field locations and messages only: pydantic would echo the offending input.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in getattr(exc, "errors", lambda: [])()
        )
        raise ProfileError(f"profile file {path} is invalid: {problems or 'bad value'}") from None
