"""The service profile: loading, validation, and the example in the repository."""

# ruff: noqa: RUF001  (Russian phrases by design)

from pathlib import Path

import pytest

from app.drafts.profile import ProfileError, ServiceProfile, load_profile
from tests.conftest import ROOT

FIXTURE = Path(__file__).parent / "fixtures" / "profile.yaml"

VALID = """\
name: Chit Trip
aliases: [Чит Трип]
countries: [Испания]
services: [жильё]
facts: [Без предоплаты]
disclosure_phrases: [сотрудничаю]
reviews:
  - id: one
    text: "Секретный текст отзыва"
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_fixture_profile_loads() -> None:
    profile = load_profile(FIXTURE)
    assert profile.name == "Chit Trip"
    assert profile.names == ("Chit Trip", "Чит Трип")
    assert profile.countries == ("Индонезия", "Таиланд", "ОАЭ", "Черногория", "Испания")
    assert profile.review("villa") is not None
    assert profile.review("nope") is None
    assert profile.review(None) is None


def test_example_profile_loads_and_has_only_placeholder_reviews() -> None:
    profile = load_profile(ROOT / "profile.example.yaml")
    assert profile.reviews
    assert all(review.text.startswith("ЗАГЛУШКА") for review in profile.reviews)
    # The example discloses the tie, not just a recommendation.
    assert "сотрудничаю" in profile.disclosure_phrases


def test_names_do_not_repeat(tmp_path: Path) -> None:
    profile = load_profile(_write(tmp_path, VALID.replace("[Чит Трип]", "[Chit Trip, Чит Трип]")))
    assert profile.names == ("Chit Trip", "Чит Трип")


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="not found"):
        load_profile(tmp_path / "missing.yaml")


@pytest.mark.parametrize(
    ("text", "where"),
    [
        (VALID.replace("countries: [Испания]", "countries: []"), "countries"),
        (VALID.replace("facts: [Без предоплаты]\n", ""), "facts"),
        (
            VALID.replace("disclosure_phrases: [сотрудничаю]", "disclosure_phrases: []"),
            "disclosure",
        ),
        (VALID.replace("id: one", "id: 'bad id!'"), "reviews.0.id"),
        (VALID + "  - id: one\n    text: другой\n", "unique"),
        (VALID + "unknown: 1\n", "unknown"),
        ("- a list\n", "mapping"),
        ("name: [unclosed\n", "cannot be read"),
    ],
)
def test_invalid_profiles_are_refused(tmp_path: Path, text: str, where: str) -> None:
    with pytest.raises(ProfileError, match=where) as caught:
        load_profile(_write(tmp_path, text))
    # The error names the problem, never the file's contents (customer reviews).
    assert "Секретный текст отзыва" not in str(caught.value)


def test_errors_do_not_echo_review_text(tmp_path: Path) -> None:
    text = VALID.replace("id: one", "id: 'Секретный текст отзыва'")
    with pytest.raises(ProfileError) as caught:
        load_profile(_write(tmp_path, text))
    assert "Секретный" not in str(caught.value)
    assert caught.value.__cause__ is None


def test_profile_is_frozen() -> None:
    profile = load_profile(FIXTURE)
    with pytest.raises(ValueError):
        profile.name = "Other"  # type: ignore[misc]
    assert isinstance(profile, ServiceProfile)


def test_advocacy_phrases_do_not_count_as_disclosure(tmp_path: Path) -> None:
    text = VALID.replace(
        "disclosure_phrases: [сотрудничаю]",
        "disclosure_phrases: [сотрудничаю, работаю с, советую сервис, рекомендую сервис]",
    )
    profile = load_profile(_write(tmp_path, text))
    assert profile.disclosures == ("сотрудничаю", "работаю с")
    assert profile.ignored_phrases == ("советую сервис", "рекомендую сервис")


def test_a_profile_without_a_real_disclosure_is_refused(tmp_path: Path) -> None:
    text = VALID.replace(
        "disclosure_phrases: [сотрудничаю]", "disclosure_phrases: [советую сервис]"
    )
    with pytest.raises(ProfileError, match="disclosure_phrases"):
        load_profile(_write(tmp_path, text))
