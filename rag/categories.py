CANONICAL_CATEGORIES = (
    "food",
    "culture",
    "nature",
    "sight",
    "entertainment",
    "mall",
    "station",
    "sport",
)

CATEGORY_LABELS = {
    "food": "еда: рестораны, кафе, бары, пекарни",
    "culture": "культура: музеи, театры, филармония",
    "nature": "парки, скверы, сады и смотровые площадки",
    "sight": "достопримечательности, храмы и памятники",
    "entertainment": "развлечения: кино, квесты, зоопарк, концерты",
    "mall": "торговый центр",
    "station": "вокзал или аэропорт",
    "sport": "спорт: бассейн, фитнес, скалодром",
}

BLACKLIST_PARTS = (
    "бухгалтер",
    "лизинг",
    "автосервис",
    "шиномонтаж",
    "склад",
    "оптом",
)

WHITELIST = {
    "Кофейня": "food",
    "Кафе": "food",
    "Ресторан": "food",
    "Пекарня": "food",
    "Быстрое питание": "food",
    "Пиццерия": "food",
    "Бар": "food",
    "Суши-бар": "food",
    "Кондитерская": "food",
    "Кофе с собой": "food",
    "Паб": "food",
    "Кальян-бар": "food",
    "Рюмочная": "food",
    "Музей": "culture",
    "Художественная галерея": "culture",
    "Театр": "culture",
    "Филармония": "culture",
    "Культурный центр": "culture",
    "Дом культуры": "culture",
    "Выставочный центр": "culture",
    "Библиотека": "culture",
    "Городской парк": "nature",
    "Парк": "nature",
    "Сквер": "nature",
    "Сад": "nature",
    "Ботанический сад": "nature",
    "Набережная": "nature",
    "Смотровая площадка": "nature",
    "Храм": "sight",
    "Собор": "sight",
    "Церковь": "sight",
    "Часовня": "sight",
    "Монастырь": "sight",
    "Памятник": "sight",
    "Мемориал": "sight",
    "Достопримечательность": "sight",
    "Квесты": "entertainment",
    "Антикафе": "entertainment",
    "Кинотеатр": "entertainment",
    "Зоопарк": "entertainment",
    "Океанариум": "entertainment",
    "Цирк": "entertainment",
    "Концертный зал": "entertainment",
    "Развлекательный центр": "entertainment",
    "Настольные и интеллектуальные игры": "entertainment",
    "Клуб виртуальной реальности": "entertainment",
    "Торговый центр": "mall",
    "Вокзал": "station",
    "Железнодорожный вокзал": "station",
    "Автовокзал": "station",
    "Автостанция": "station",
    "Аэропорт": "station",
    "Бассейн": "sport",
    "Фитнес-клуб": "sport",
    "Фитнес-центр": "sport",
    "Спортивный комплекс": "sport",
    "Скалодромы": "sport",
    "Спортивное объединение": "sport",
}


def classify_rubric(rubric: str | None) -> tuple[str | None, str]:
    if rubric is None or not str(rubric).strip():
        return None, "missing_type"
    text = str(rubric).strip()
    lowered = text.casefold()
    if any(part in lowered for part in BLACKLIST_PARTS):
        return None, "blacklist"
    category = WHITELIST.get(text)
    if category is None:
        return None, "not_in_whitelist"
    return category, "kept"
