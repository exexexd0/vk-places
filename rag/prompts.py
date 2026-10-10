from rag.categories import CATEGORY_LABELS

PLAN_ROUTE_SYSTEM = """Ты составляешь план городского маршрута.
По запросу пользователя выбери только категории из переданного списка.
Не выбирай конкретные места, адреса и идентификаторы.
Каждая отдельная цель пользователя становится одной категорией.
Категорию нельзя повторять и нельзя добавлять, если её нет в доступном списке.
Порядок категорий в массиве не задаёт порядок посещения.
precedence заполняй только если пользователь явно сказал, что одно должно быть раньше другого.
fixed_first — категория, с которой маршрут обязан начаться. Иначе null.
fixed_last — категория, которой маршрут обязан закончиться. Иначе null.
Заголовок маршрута — короткая русская фраза без кавычек."""

PLAN_ROUTE_JSON_SUFFIX = (
    "Ответь одним JSON-объектом с ключами route_title, required_points, precedence, fixed_first, fixed_last. "
    "required_points — массив кодов категорий. "
    "precedence — массив объектов {before, after}. "
    "fixed_first и fixed_last — код категории или null."
)

JEV_INSTRUCTIONS = (
    "Учитывая запрос пользователя и город, оцени, "
    "какую из переданных точек интереса лучше включить в маршрут для этой категории. "
    "Сравнивай только эти варианты и не придумывай новые места."
)


def plan_route_user(user_query: str, city: str, available_categories: list[str]) -> str:
    available = ", ".join(
        f"{category} ({CATEGORY_LABELS[category]})" for category in available_categories
    )
    return (
        f"Город: {city}\n"
        f"Запрос: {user_query}\n"
        f"Доступные категории: {available}\n"
        "Выбери от 1 до 4 категорий. Порядок массива не задаёт порядок посещения."
    )


def retrieval_query(user_query: str, category: str) -> str:
    return "\n".join(
        [
            user_query,
            f"Категория: {CATEGORY_LABELS[category]}",
        ]
    )


def candidate_criterion(candidate: dict) -> str:
    rating = candidate.get("rating")
    rating_text = "нет оценки" if rating is None else str(rating)
    return (
        f"{candidate.get('name')}. "
        f"Категория: {CATEGORY_LABELS.get(candidate.get('category'), candidate.get('category'))}. "
        f"Адрес: {candidate.get('address')}. "
        f"Рейтинг: {rating_text}."
    )
