# ruff: noqa: RUF001, RUF003  (Russian reply texts by design)

from pathlib import Path

import pytest

from app.drafts.profile import load_profile
from app.services.guard import MAX_DRAFT_LENGTH, check_draft


@pytest.mark.parametrize(
    ("body", "issues"),
    [
        ("Здравствуйте! Подскажите, когда вам удобно созвониться?", []),
        ("Стоимость 45 000 руб. в месяц", ["price"]),
        ("Цена 30000р. без комиссии", ["price"]),
        ("Всего 50к", ["price"]),
        ("Аренда 1200 $", ["price"]),
        ("€900 per month", ["price"]),
        ("Залог 2 тыс", ["price"]),
        ("Стоимость 45 000 ₽", ["price"]),
        ("Это на ул. Ленина 5", ["address"]),
        ("Находится на улице Пушкина, 10", ["address"]),
        ("Проспект Мира 12", ["address"]),
        ("Дом 7, рядом метро", ["address"]),
        ("д. 3, кв. 15", ["address"]),
        ("Показ 12.10 вечером", ["date"]),
        ("Заезд с 5/11", ["date"]),
        ("Можно 15 октября", ["date"]),
        ("С 1-го марта", ["date"]),
        ("Квартира свободна", ["availability"]),
        ("Есть в наличии две студии", ["availability"]),
        ("Она уже забронирована", ["availability"]),
        ("Свободно с понедельника", ["availability"]),
        (
            "Ещё свободна, 40 000 руб, ул. Мира 3, с 1 мая",
            ["price", "address", "date", "availability"],
        ),
        ("Ёлки, она СВОБОДНА", ["availability"]),
        ("Улица тихая, дома рядом новые", []),  # address words without a number
        ("У нас 3 комнаты", []),  # "к" only as a currency suffix
    ],
)
def test_issues(body: str, issues: list[str]) -> None:
    assert check_draft(body) == issues


def test_length() -> None:
    assert check_draft("а" * MAX_DRAFT_LENGTH) == []
    assert check_draft("а" * (MAX_DRAFT_LENGTH + 1)) == ["too_long"]


@pytest.mark.parametrize("source", ["llm", "manual"])
def test_non_template_sources_are_checked(source: str) -> None:
    assert check_draft("Цена 100 руб", source) == ["price"]


def test_owner_approved_templates_are_not_checked() -> None:
    assert check_draft("Цена 100 руб, свободна с 12.10" + "!" * 2000, "template") == []


# --- P5: the advertising rules ------------------------------------------------------

PROFILE = load_profile(Path(__file__).parent / "fixtures" / "profile.yaml")
DISCLOSED = "Советую сервис Chit Trip, с которым сотрудничаю: жильё напрямую от собственников."
VILLA = PROFILE.reviews[0].text


@pytest.mark.parametrize(
    "body",
    [
        DISCLOSED + " Подскажите, какой город вам интересен?",
        "Я работаю с Чит Трип и могу помочь найти выгодно. Какие даты поездки вам удобны?",
        f"Представляю сервис Chit Trip. Клиенты пишут: «{VILLA}»",
        # A word-for-word fragment, also with an ellipsis around it.
        DISCLOSED + " Из отзыва: «хозяин очень приветливый»",
        DISCLOSED + " «…Билеты оформили быстро и без лишних вопросов…»",
        # A quoted name is a name, not a review.
        "Сотрудничаю с сервисом «Chit Trip»: без предоплаты.",
        # The customer's story is fine inside the quote.
        "Работаю с Chit Trip. «Нашли виллу у моря за один вечер, хозяин очень приветливый.»",
        "Мы подберём жильё без предоплаты, я сотрудничаю с Chit Trip.",
    ],
)
def test_correct_draft_passes(body: str) -> None:
    assert check_draft(body, "llm", PROFILE) == []


@pytest.mark.parametrize(
    "body",
    [
        "Советую сервис Chit Trip, там без предоплаты.",  # a recommendation is not a disclosure
        "Я сотрудничаю с хорошим сервисом, напишите мне.",  # no name
        "Отличный сервис Chit Trip! «Сотрудничаю»",  # the phrase only inside quotes
        "Отличный сервис! «Я сотрудничаю с Chit Trip»",  # disclosure only inside quotes
    ],
)
def test_no_disclosure(body: str) -> None:
    assert "no_disclosure" in check_draft(body, "llm", PROFILE)


@pytest.mark.parametrize(
    "body",
    [
        # Negated: an explicit denial of the tie.
        "Я не сотрудничаю с Chit Trip, просто советую: жильё без предоплаты.",
        "Я никогда не работаю с Chit Trip, но советую их.",
        # Third person: "сотрудничаю" is a substring of "сотрудничают".
        "Многие сотрудничают с Chit Trip, советую их: жильё без предоплаты.",
        # The phrase is not about the service.
        "Я работаю с утра до ночи, но советую Chit Trip: жильё без предоплаты.",
        # The name and the phrase are in different sentences.
        "Советую Chit Trip. Я работаю с агентствами.",
    ],
)
def test_disclosure_must_be_first_person_and_about_the_service(body: str) -> None:
    assert "no_disclosure" in check_draft(body, "llm", PROFILE)


@pytest.mark.parametrize(
    "body",
    [
        "Сервис Chit Trip, с которым я сотрудничаю, поможет найти выгодно.",
        "Добрый день! Я сотрудничаю с сервисом Chit Trip и могу помочь.",
        "Я работаю с Chit Trip, мы с удовольствием подберём виллу без предоплаты.",
        "Мы с вами найдём вариант. Я представляю сервис Chit Trip.",
    ],
)
def test_disclosure_forms_that_pass(body: str) -> None:
    assert check_draft(body, "llm", PROFILE) == []


def test_advocacy_phrases_of_a_profile_disclose_nothing() -> None:
    weak = PROFILE.model_copy(
        update={"disclosure_phrases": (*PROFILE.disclosure_phrases, "советую сервис")}
    )
    body = "Советую сервис Chit Trip, там жильё напрямую от собственников."
    assert check_draft(body, "llm", weak) == ["no_disclosure"]


@pytest.mark.parametrize(
    "body",
    [
        "Мы сами бронировали через Chit Trip, очень довольны.",
        "Я тоже снимала там квартиру.",
        "Летали с мужем в Таиланд — всё отлично.",
        "Мы уже забронировали так виллу на Бали.",
        "Нам подобрали квартиру за день.",
        "Я лично пользовалась этим сервисом.",
        "Сами брали билеты, без проблем.",
        "Наша поездка прошла отлично.",
        "Как клиент скажу: рекомендую.",
        "Ёлки, мы жили там месяц.",
        # A companion in the instrumental case, also one not in the list.
        "Мы с сестрой снимали там дом.",
        "Мы с Олегом летали туда весной.",
        # Words between the pronoun and the verb.
        "Я в прошлом году снимала там дом.",
        # A story without a pronoun.
        "Отдыхали на Пхукете в прошлом году, всё понравилось.",
        # Present tense.
        "Мы всегда бронируем жильё через них.",
        "Я сама пользуюсь их услугами.",
        # "наш/мой опыт".
        "Наш опыт был отличный.",
        "По моему опыту, это лучший вариант.",
    ],
)
def test_customer_claims_outside_quotes(body: str) -> None:
    assert "customer_claim" in check_draft(DISCLOSED + " " + body, "llm", PROFILE)
    # Without the profile too: posing as a customer is never fine.
    assert "customer_claim" in check_draft(body, "manual")


@pytest.mark.parametrize(
    ("body", "issue"),
    [
        ("Скидка 10% для вас", "percent"),
        ("Скидка десять процентов", "percent"),
        ("Можно снять за полцены", "half_price"),
        ("Почти за пол-цены", "half_price"),
        ("Половину стоимости вернут", "half_price"),
        ("В 2 раза дешевле, чем у агентств", "times_comparison"),
        ("В полтора раза выгоднее", "times_comparison"),
        ("Вдвое дешевле рынка", "times_comparison"),
        ("Всего 500 долларов в месяц", "price"),
        ("От 300 евро", "price"),
        ("15000 бат за месяц", "price"),
        ("2000 AED", "price"),
        ("Около 3 млн рупий", "price"),
        # Amounts in words.
        ("Вилла обойдётся примерно в тридцать тысяч рублей.", "price"),
        ("Вилла за пятьсот долларов.", "price"),
        ("Это стоит пару миллионов рупий", "price"),
        ("Цены в евро и батах", "price"),
    ],
)
def test_numbers_and_comparisons(body: str, issue: str) -> None:
    assert issue in check_draft(DISCLOSED + " " + body, "llm", PROFILE)
    assert issue in check_draft(body)


@pytest.mark.parametrize(
    "body",
    [
        "Евроремонт и 3 батареи",  # currency-looking words that are not currencies
        "Несколько раз помогали с жильём",  # "раз" without "в N раз"
        "Рубрика полезных советов",  # "руб" inside a word
        "Мы с вами подберём вариант, я с удовольствием помогу",  # not companions
    ],
)
def test_no_false_alarm_on_similar_words(body: str) -> None:
    assert check_draft(body) == []


@pytest.mark.parametrize(
    "quote",
    [
        "Нашли виллу у моря за один вечер, хозяин очень приветливый! Спасибо!",  # changed
        "Нашли виллу у моря за час",  # invented
        "Лучший сервис, всем советую",  # not in the profile at all
        "",
    ],
)
def test_misquote(quote: str) -> None:
    assert "misquote" in check_draft(f"{DISCLOSED} «{quote}»", "llm", PROFILE)


@pytest.mark.parametrize("marks", [("“", "”"), ('"', '"'), ("„", "“")])
def test_other_quote_marks_are_quotes_too(marks: tuple[str, str]) -> None:
    left, right = marks
    assert check_draft(f"{DISCLOSED} {left}{VILLA}{right}", "llm", PROFILE) == []
    assert "misquote" in check_draft(f"{DISCLOSED} {left}Придумано{right}", "llm", PROFILE)


def test_quote_whitespace_is_normalized() -> None:
    spaced = VILLA.replace(" ", "  \n", 2)
    assert check_draft(f"{DISCLOSED} «{spaced}»", "llm", PROFILE) == []


def test_at_most_one_quote() -> None:
    tickets = PROFILE.reviews[1].text
    issues = check_draft(f"{DISCLOSED} «{VILLA}» и ещё «{tickets}»", "llm", PROFILE)
    assert issues == ["too_many_quotes"]


def test_profile_rules_need_the_profile() -> None:
    assert check_draft("Советую этот сервис. «Придумано»") == []
    assert check_draft("Советую этот сервис. «Придумано»", "llm", PROFILE) == [
        "no_disclosure",
        "misquote",
    ]


def test_templates_skip_the_profile_rules_too() -> None:
    assert check_draft("Мы сами бронировали, скидка 50%", "template", PROFILE) == []


def test_all_issues_together() -> None:
    body = "Мы сами бронировали за 45 000 руб., в 2 раза дешевле! «Выдумка» и «ещё»"
    assert check_draft(body, "llm", PROFILE) == [
        "price",
        "times_comparison",
        "customer_claim",
        "no_disclosure",
        "too_many_quotes",
        "misquote",
    ]
