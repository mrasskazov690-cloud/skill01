"""Auto-mode guard: drafts that state facts (prices, addresses, dates, availability)
go to the owner instead of being sent. False positives only cost a review."""

# ruff: noqa: RUF001, RUF003  (the patterns are Russian text by design)

import re

from app.domain.states import DraftSource

MAX_DRAFT_LENGTH = 1000

_NUM = r"\d+(?:[.,\s]\d+)*"
_MONTHS = r"(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*"
# Matched against the lower-cased body with ё folded into е.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "price": re.compile(rf"{_NUM}\s*(?:руб[а-я]*|р\.|₽|\$|€|тыс[а-я]*\.?|т\.р|к\b|k\b)|[₽$€]\s*\d"),
    "address": re.compile(
        r"(?<![а-я])(?:"
        r"(?:ул\.?|улиц[а-я]*|пр-т|проспект[а-я]*)\s*[^\d\n]{0,40}?\d"
        r"|(?:дом[а-я]*|д\.|кв\.?|квартир[а-я]*\s*№)\s*№?\s*\d"
        r")"
    ),
    "date": re.compile(
        rf"(?<!\d)\d{{1,2}}[./]\d{{1,2}}(?!\d)|\d{{1,2}}\s*(?:-?го\s+)?{_MONTHS}"
        rf"|(?<![а-я]){_MONTHS}\s+\d{{1,2}}(?!\d)"
    ),
    "availability": re.compile(
        r"(?<![а-я])(?:свободн[а-я]*|в наличии|забронирован[а-я]*)(?![а-я])"
    ),
}


def check_draft(body: str, source: DraftSource | str = DraftSource.LLM) -> list[str]:
    """Issue codes that keep a draft from being sent automatically (empty: ok).

    Templates the owner approved are trusted as they are and are not checked.
    """
    if DraftSource(source) is DraftSource.TEMPLATE:
        return []
    text = body.lower().replace("ё", "е")
    issues = [code for code, pattern in _PATTERNS.items() if pattern.search(text)]
    if len(body) > MAX_DRAFT_LENGTH:
        issues.append("too_long")
    return issues
