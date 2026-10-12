"""Auto-mode guard: drafts that state facts (prices, addresses, dates, availability,
discounts) or break the advertising rules go to the owner instead of being sent.
False positives only cost a review.

With the service profile the draft must also disclose the tie to the service (one of
its disclosure phrases, in the first person and not negated, tied to its name in one
sentence, outside quotes) and may quote at most one review, word for word. Telling a
customer's story as one's own ("мы сами бронировали", "летали с мужем") outside quotes
is never allowed.
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
        # Amounts in words ("тридцать тысяч рублей", "пятьсот долларов"): a currency or
        # a "thousand/million" never belongs in a draft, with or without a digit.
        r"|[₽$€]|(?<![а-я])(?:руб(?:\.|л[а-я]*)?|доллар[а-я]*|евро|бат(?:а|ов|ы)?"
        r"|дирхам[а-я]*|рупи[а-я]*|тысяч[а-я]*|тыс\.|млн|млрд|миллион[а-я]*"
        r"|миллиард[а-я]*)(?![а-я-])"
        r"|(?<![a-z])(?:usd|eur|aed|thb|idr|rub)(?![a-z])"
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
# First-person stories of a customer: "мы бронировали", "я в прошлом году снимала",
# "мы всегда бронируем", "мы с сестрой", "отдыхали на Пхукете", "наш опыт".
_CLIENT_VERBS = (
    r"(?:за)?бронир|снима|сня|арендова|(?:с)?лета|(?:съ)?езди|(?:про)?жи|останавлива"
    r"|останови|отдыха|покупа|купи|бра|взя|заказ|(?:вос)?пользова|обраща|обрати|оформ"
)
_CLIENT_PAST = rf"(?:{_CLIENT_VERBS})[а-я]*?(?:л|ла|ли|ло)(?:сь)?(?![а-я])"
_CLIENT_PRESENT = (
    r"(?:(?:за)?бронирую|(?:за)?бронируем|снимаю|снимаем|арендую|арендуем|пользуюсь"
    r"|пользуемся|летаю|летаем|езжу|ездим|отдыхаю|отдыхаем|заказываю|заказываем"
    r"|обращаюсь|обращаемся|останавливаюсь|останавливаемся|покупаю|покупаем)(?![а-я])"
)
# "мы с вами", "я с удовольствием": not a companion.
_NOT_COMPANIONS = r"вами|тобой|удовольствием|радостью|интересом|нетерпением|пониманием|уважением"
_CUSTOMER_CLAIM = re.compile(
    # A pronoun, up to four words, a client verb in the past or present tense.
    r"(?<![а-я])(?:я|мы|сам|сама|сами)\s+(?:[а-я-]+\s+){0,4}?"
    rf"(?:{_CLIENT_PAST}|{_CLIENT_PRESENT})"
    # A sentence that starts with a client verb: "Отдыхали на Пхукете…".
    rf"|(?:^|[.!?…]\s+|\n\s*)(?:(?:тоже|уже|сами|сама|сам|недавно|как-то)\s+)?"
    rf"{_CLIENT_PAST}"
    r"|(?<![а-я])(?:с|вместе с)\s+(?:мужем|женой|супругом|супругой|семьей|детьми|ребенком"
    r"|подругой|подругами|другом|друзьями|парнем|девушкой|мамой|папой|родителями"
    r"|сестрой|братом|сыном|дочкой|дочерью|коллегой|коллегами)(?![а-я])"
    # "мы с <anyone>" (instrumental case): a companion.
    rf"|(?<![а-я])(?:я|мы)\s+с\s+(?!(?:{_NOT_COMPANIONS})(?![а-я]))"
    r"[а-я]+(?:ой|ей|ом|ем|ами|ями|ым|им)(?![а-я])"
    r"|(?<![а-я])(?:мне|нам)\s+(?:(?:тоже|уже|там|быстро|очень)\s+)?"
    r"(?:помог|подобрал|нашл|нашел|забронировал|оформил|посоветовал|сдал)[а-я]*"
    r"|(?<![а-я])(?:наш|наша|нашу|нашей|нашему|нашим|мой|моя|мою|моей|моему|моим)\s+"
    r"(?:отпуск|поездк|отдых|семь|опыт)"
    r"|(?<![а-я])как\s+(?:клиент|покупател)"
)
# Sentences: a dot that ends one is followed by a space ("chittrip.ru" is one word).
_SENTENCE_END = re.compile(r"[.!?…]+(?=\s|$)|\n")
_WORD = r"[^\s.,:;!?…—–()-]+"
# A disclosure must not be negated: "я не сотрудничаю", "никогда не работаю с".
_NEGATION = re.compile(r"(?<![а-яa-z])(?:не|ни|никогда|нигде)\s+(?:[а-я]+\s+)?$")


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
    _, outside = _split_quotes(body)
    if _CUSTOMER_CLAIM.search(_fold(outside)):
        issues.append("customer_claim")
    if profile is not None:
        issues.extend(_profile_issues(body, profile))
    if len(body) > MAX_DRAFT_LENGTH:
        issues.append("too_long")
    return issues


def _fold(text: str) -> str:
    return text.lower().replace("ё", "е")


def _split_quotes(body: str, names: frozenset[str] = frozenset()) -> tuple[list[str], str]:
    """The quoted segments and the text outside them. A quoted name ("сервис
    «Chit Trip»") is a name, not a quote: it stays in the text, without the marks."""
    quotes: list[str] = []

    def outside(match: re.Match[str]) -> str:
        quote = next(g for g in match.groups() if g is not None)
        if _fold(_normalize(quote)) in names:
            return quote
        quotes.append(quote)
        return " "

    return quotes, _QUOTES.sub(outside, body)


def _normalize(text: str) -> str:
    return _SPACES.sub(" ", text).strip()


def _profile_issues(body: str, profile: "ServiceProfile") -> list[str]:
    issues: list[str] = []
    names = frozenset(_fold(name) for name in profile.names)
    cited, outside = _split_quotes(body, names)
    if not _discloses(_fold(outside), names, profile.disclosures):
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


def _discloses(text: str, names: frozenset[str], phrases: tuple[str, ...]) -> bool:
    """A first-person, not negated disclosure phrase tied to the service's name in one
    sentence: "работаю с Chit Trip", "сотрудничаю с сервисом Chit Trip" (the name at
    most three words after the phrase) or "сервис Chit Trip, с которым сотрудничаю"
    (the name before it, joined by "который"/"где")."""
    name_re = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    for sentence in _SENTENCE_END.split(_SPACES.sub(" ", text)):
        for phrase in phrases:
            phrase_re = re.escape(_fold(_normalize(phrase))).replace(r"\ ", r"\s+")
            for match in re.finditer(rf"(?<![а-яa-z0-9]){phrase_re}(?![а-яa-z0-9])", sentence):
                if _NEGATION.search(sentence[: match.start()]):
                    continue
                after = sentence[match.end() :]
                before = sentence[: match.start()]
                if re.match(
                    rf"[\s,:—–-]*(?:{_WORD}[\s,:—–-]+){{0,3}}?(?:{name_re})(?![а-яa-z0-9])",
                    after,
                ) or re.search(
                    rf"(?<![а-яa-z0-9])(?:{name_re})[\s,:—–-]+(?:{_WORD}\s+){{0,2}}"
                    r"(?:котор[а-я]*|где)\s+(?:я\s+)?(?:сам[а-я]?\s+|лично\s+)?$",
                    before,
                ):
                    return True
    return False
