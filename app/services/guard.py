"""Auto-mode guard: drafts that state facts (prices, addresses, dates, availability,
discounts) or break the advertising rules go to the owner instead of being sent.
False positives only cost a review.

With the service profile the draft must also disclose the tie to the service (one of
its disclosure phrases and its name, outside quotes) and may quote at most one review,
word for word. Telling a customer's story as one's own ("мы сами бронировали",
"летали с мужем") outside quotes is never allowed.
"""

# ruff: noqa: RUF001, RUF002, RUF003  (the patterns are Russian text by design)

import re
from typing import TYPE_CHECKING

from app.domain.states import DraftSource

if TYPE_CHECKING:
    from app.drafts.profile import ServiceProfile

MAX_DRAFT_LENGTH = 1000

_NUM = r"\d+(?:[.,\s]\d+)*"
_MONTHS = r"(?:январ|феврал|март|апрел|ма[йя]|июн|июл|август|сентябр|октябр|ноябр|декабр)[а-я]*"
# Matched against the lower-cased body with ё folded into е.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "price": re.compile(
        rf"{_NUM}\s*(?:(?:млн|млрд)[а-я]*\.?\s*)?(?:руб[а-я]*|р\.|₽|\$|€|тыс[а-я]*\.?|т\.р|к\b|k\b"
        r"|долл[а-я]*|евро(?![а-я])|бат(?:а|ов)?(?![а-я])|дирхам[а-я]*|рупи[а-я]*"
        r"|(?:usd|eur|aed|thb|idr|rub)(?![a-z]))"
        r"|[₽$€]\s*\d"
    ),
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
    # Discounts and comparisons are numbers too: "скидка 10%", "в 2 раза дешевле".
    "percent": re.compile(r"%|(?<![а-я])процент"),
    "half_price": re.compile(
        r"(?<![а-я])пол\s*-?\s*цены|(?<![а-я])половин[а-я]*\s+(?:цены|стоимост)"
    ),
    "times_comparison": re.compile(
        r"(?<![а-я])в\s+(?:\d+(?:[.,]\d+)?|полтора|полторы|два|две|три|четыре|пять|десять"
        r"|несколько)\s+раз(?:а)?(?![а-я])"
        r"|(?<![а-я])(?:вдвое|втрое|вчетверо|впятеро|вдесятеро)(?![а-я])"
    ),
}

# Quoted segments: «…», “…”, „…“ and "…". Reviews may only appear inside them.
_QUOTES = re.compile(r"«([^»]*)»|“([^”]*)”|„([^“”]*)[“”]|\"([^\"]*)\"")
_ELLIPSIS = re.compile(r"^(?:\.\.\.|…)\s*|\s*(?:\.\.\.|…)$")
_SPACES = re.compile(r"\s+")
# Past-tense first-person stories of a customer: "мы бронировали", "я сама снимала".
_CLIENT_VERBS = (
    r"(?:за)?бронир|снима|сня|арендова|(?:с)?лета|(?:съ)?езди|(?:про)?жи|останавлива"
    r"|останови|отдыха|покупа|купи|бра|взя|заказ|(?:вос)?пользова|обраща|обрати|оформ"
)
_CUSTOMER_CLAIM = re.compile(
    r"(?<![а-я])(?:я|мы|сам|сама|сами)\s+"
    r"(?:(?:сам|сама|сами|уже|тоже|недавно|там|лично|не раз|неоднократно|как-то)\s+){0,3}"
    rf"(?:{_CLIENT_VERBS})[а-я]*?(?:л|ла|ли|ло)(?:сь)?(?![а-я])"
    r"|(?<![а-я])(?:с|вместе с)\s+(?:мужем|женой|супругом|супругой|семьей|детьми|ребенком"
    r"|подругой|другом|парнем|девушкой|мамой|родителями)(?![а-я])"
    r"|(?<![а-я])(?:мне|нам)\s+(?:(?:тоже|уже|там|быстро|очень)\s+)?"
    r"(?:помог|подобрал|нашл|нашел|забронировал|оформил|посоветовал|сдал)[а-я]*"
    r"|(?<![а-я])(?:наш|наша|нашу|нашей|мой|моя|мою|моей)\s+(?:отпуск|поездк|отдых|семь)"
    r"|(?<![а-я])как\s+(?:клиент|покупател)"
)


def check_draft(
    body: str,
    source: DraftSource | str = DraftSource.LLM,
    profile: "ServiceProfile | None" = None,
) -> list[str]:
    """Issue codes that keep a draft from being sent automatically (empty: ok).

    Templates the owner approved are trusted as they are and are not checked. With
    `profile`, the disclosure and quotation rules are checked too.
    """
    if DraftSource(source) is DraftSource.TEMPLATE:
        return []
    text = _fold(body)
    issues = [code for code, pattern in _PATTERNS.items() if pattern.search(text)]
    quotes, outside = _split_quotes(body)
    if _CUSTOMER_CLAIM.search(_fold(outside)):
        issues.append("customer_claim")
    if profile is not None:
        issues.extend(_profile_issues(quotes, outside, profile))
    if len(body) > MAX_DRAFT_LENGTH:
        issues.append("too_long")
    return issues


def _fold(text: str) -> str:
    return text.lower().replace("ё", "е")


def _split_quotes(body: str) -> tuple[list[str], str]:
    """The quoted segments and the text outside them."""
    quotes = [next(g for g in m.groups() if g is not None) for m in _QUOTES.finditer(body)]
    return quotes, _QUOTES.sub(" ", body)


def _normalize(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def _profile_issues(quotes: list[str], outside: str, profile: "ServiceProfile") -> list[str]:
    issues: list[str] = []
    folded = _fold(outside)
    names = {_fold(name) for name in profile.names}
    # A quoted name ("сервис «Chit Trip»") is a name, not a review.
    quoted_names = [q for q in quotes if _fold(_normalize(q)) in names]
    cited = [q for q in quotes if _fold(_normalize(q)) not in names]
    discloses = any(_fold(p) in folded for p in profile.disclosure_phrases)
    named = bool(quoted_names) or any(name in folded for name in names)
    if not (discloses and named):
        issues.append("no_disclosure")
    if len(cited) > 1:
        issues.append("too_many_quotes")
    reviews = [_normalize(review.text) for review in profile.reviews]
    for quote in cited:
        fragment = _normalize(_ELLIPSIS.sub("", _normalize(quote)))
        if not fragment or not any(fragment in review for review in reviews):
            issues.append("misquote")
            break
    return issues
