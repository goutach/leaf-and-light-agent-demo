"""Подбор по каталогу и расчет месячной аренды."""

import json
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path


CATALOGUE = json.loads(Path(__file__).with_name("catalogue.json").read_text(encoding="utf-8"))
MINIMUM = CATALOGUE["minimum"]
UNIT_PRICE = min(product["monthly"] for product in CATALOGUE["products"])
SOURCE = "Каталог офисных растений"

NUMBERS = {
    "один": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
    "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
}
NUM = r"(?:[1-9]\d{0,2}|один|одно|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять)"
UNITS = r"(?:растени[еяй]|цвет(?:ок|ка|ков|ы)|кашпо|штук[аи]?|шт)"
QUANTITY = re.compile(rf"\b({NUM})\s+{UNITS}\b")
CHANGE = re.compile(rf"(?:а если|если|тогда|давайте|оставим|лучше)\s+({NUM})(?:\s+{UNITS})?[.!?]?")
BARE_QUANTITY = re.compile(rf"({NUM})[.!?]?")
BUDGET = re.compile(r"\b(бюджет(?:\s+до)?|до|не больше|в пределах)\s*[:—-]?\s*"
                    r"(\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d{1,7})(?:\s*(тыс\.?|тысяч[аи]?))?"
                    r"(?:\s*(₽|руб\w*))?(?!\d)")
REQUEST = re.compile(r"\b(?:нужн(?:о|ы|а|ен)(?!\s+ли)|хотим|хочу|подбер\w*|подобра\w*|рассчита\w*|"
                     r"сколько\s+(?:будет\s+стоить|выйдет|за)|во сколько)\b")
RESET_BUDGET = re.compile(r"\b(?:без лимита|без ограничения по бюджету|бюджет не важен)\b")
NO_LIGHT = re.compile(r"\b(?:совсем без света|нет света|без естественного и искусственного света)\b")
DIFFUSE = re.compile(r"\b(?:рассеянн\w*\s+(?:дневн\w*\s+)?свет\w*|свет\s+рассеянн\w*)\b")
BRIGHT = re.compile(r"\b(?:много света|светл\w+\s+(?:офис|комната|переговорная)|хорошо освещен\w*)\b")


@dataclass(frozen=True, slots=True)
class Brief:
    quantity: int | None = None
    light: str | None = None
    budget: int | None = None


def rubles(value):
    return f"{value:,}".replace(",", " ") + " ₽"


def parse_number(value):
    if value.isdecimal():
        return int(value)
    return NUMBERS[value]


def read_brief(message, prior):
    text = message.casefold().replace("ё", "е").strip()
    quantity_matches = list(QUANTITY.finditer(text))
    quantities = {parse_number(row[1]) for row in quantity_matches}
    revision = CHANGE.fullmatch(text) if prior else None
    if prior and prior.quantity is None:
        revision = revision or BARE_QUANTITY.fullmatch(text)
    budget_match = BUDGET.search(text)
    budget = None
    if budget_match and (budget_match[1].startswith("бюджет") or budget_match[3] or budget_match[4]):
        budget = int(re.sub(r"\s+", "", budget_match[2])) * (1000 if budget_match[3] else 1)
    reset_budget = bool(prior and RESET_BUDGET.search(text))
    light = ("none" if NO_LIGHT.search(text) else "diffuse" if DIFFUSE.search(text)
             else "bright" if BRIGHT.search(text) else None)
    starts_selection = bool((quantities or "растени" in text or "кашпо" in text)
                            and (REQUEST.search(text) or light or budget is not None))
    bare_quantity = QUANTITY.fullmatch(text.rstrip(" .!?"))
    continues_selection = bool(prior and (revision or bare_quantity or light or budget is not None or reset_budget))
    if not (starts_selection or continues_selection):
        return None

    brief = prior or Brief()
    if revision:
        quantities = {parse_number(revision[1])}
    # Если клиент назвал разные количества, спросим какое считать.
    quantity = next(iter(quantities)) if len(quantities) == 1 else None
    return replace(brief, quantity=quantity if quantities else brief.quantity, light=light or brief.light,
                   budget=None if reset_budget else budget if budget is not None else brief.budget)


def estimate(brief):
    """Возвращает только цены и позиции, которые рассчитаны из каталога."""
    if brief.light == "none":
        return "Совсем без света растения не ставим. В комнате можно добавить подсветку?", None
    if brief.quantity is None:
        return "Сколько растений нужно?", None
    if brief.quantity < MINIMUM:
        return f"Минимальный заказ от {MINIMUM} растений. Посчитать на это количество?", None
    if brief.light is None:
        return "Как со светом в комнате? Например, есть рассеянный свет от окна или ламп?", None

    products = sorted(
        (item for item in CATALOGUE["products"] if brief.light in item["lights"]),
        key=lambda item: item["monthly"],
    )
    cheapest = products[0]["monthly"]
    budget = brief.budget
    affordable = [item for item in products if budget is None or item["monthly"] * brief.quantity <= budget]
    options = [
        {"sku": item["sku"], "name": item["name"], "unit": item["monthly"],
         "monthly": item["monthly"] * brief.quantity}
        for item in (affordable or products[:1])
    ]
    fits = bool(affordable)
    snapshot = {
        "input": asdict(brief), "light": CATALOGUE["lights"][brief.light],
        "fits": fits, "options": options, "source": SOURCE,
    }
    if fits:
        return "По свету подходит. Кашпо и уход уже входят в аренду.", snapshot

    cost = rubles(cheapest * brief.quantity)
    maximum = budget // cheapest
    text = f"На это количество самый доступный вариант выйдет {cost} в месяц."
    if maximum >= MINIMUM:
        text += f" В ваш бюджет поместится до {maximum} шт."
    else:
        text += " В этот бюджет минимальный заказ не помещается."
    return text, snapshot


def summarize(quote):
    quantity = quote["input"]["quantity"]
    return "\n".join(f'{item["name"]} · {quantity} шт. · {rubles(item["monthly"])} в месяц'
                     for item in quote["options"])


def check_snapshot(value):
    if not isinstance(value, dict) or not isinstance(value.get("input"), dict):
        raise ValueError("Некорректный расчет")
    data = value["input"]
    quantity, light, budget = (data.get(key) for key in ("quantity", "light", "budget"))
    if (type(quantity) is not int or not MINIMUM <= quantity <= 999
            or not isinstance(light, str) or light not in CATALOGUE["lights"]
            or (budget is not None and (type(budget) is not int or not 0 <= budget <= 9_999_999_000))):
        raise ValueError("Некорректные условия расчета")
    _, expected = estimate(Brief(quantity, light, budget))
    if expected is None or value != expected:
        raise ValueError("Расчет не совпадает с каталогом")
    return expected
