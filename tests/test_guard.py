# ruff: noqa: RUF001  (Russian reply texts by design)

import pytest

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
