import json
from pathlib import Path

from rag.categories import CANONICAL_CATEGORIES, classify_rubric
from rag.documents import places_to_documents
from rag.normalize import clean_address, clean_place_name, load_places

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "Иваново.json"


def test_hours_are_removed_before_rating():
    cleaned = clean_place_name("Березка4,2Закрыто до 9:00 AM", "Кафе", 4.2)

    assert cleaned.name == "Березка"
    assert cleaned.opening_hours == "Закрыто до 9:00 AM"


def test_price_tail_and_decimal_rating():
    cleaned = clean_place_name("Вафля House4,9Капучино: от 180 ₽", "Кофейня", 4.9)

    assert cleaned.name == "Вафля House"
    assert cleaned.opening_hours is None


def test_glued_rubric_and_integer_rating():
    cleaned = clean_place_name("Манеки4,9Кафе", "Кафе", 4.9)
    street = clean_place_name("Ленина 305Капучино: от 210 ₽", "Кофейня", 5)

    assert cleaned.name == "Манеки"
    assert street.name == "Ленина 30"


def test_unrelated_number_stays_in_name():
    cleaned = clean_place_name("Ленина 30", "Кафе", 4.2)

    assert cleaned.name == "Ленина 30"


def test_repeated_rubric_and_rating_are_peeled():
    cleaned = clean_place_name("Асгард4,9БарБар", "Бар", 4.9)

    assert cleaned.name == "Асгард"


def test_repeated_hours_are_peeled():
    cleaned = clean_place_name("Торговый центрДо 19:00До 19:00", "Торговый центр", 4.1)

    assert cleaned.name == "Торговый центр"
    assert cleaned.opening_hours == "До 19:00"


def test_closed_until_weekday_and_ticket_price_are_peeled():
    closed = clean_place_name("ЛюбаваЗакрыто до пн.Закрыто до пн.", "Торговый центр", None)
    ticket = clean_place_name("Музей камня4,9Билет: от 150 ₽", "Музей", 4.9)

    assert closed.name == "Любава"
    assert closed.opening_hours == "Закрыто до пн."
    assert ticket.name == "Музей камня"


def test_bare_closed_status_is_peeled():
    cleaned = clean_place_name("Polygon GamingЗакрытоЗакрыто", "Компьютерный клуб", None)

    assert cleaned.name == "Polygon Gaming"
    assert cleaned.card_status == "Закрыто"


def test_page_dump_keeps_title_before_rating_block():
    cleaned = clean_place_name(
        "ПоискМаршрутыКарты · Иваново · Торговые центрыАльянсРейтинг 4,2"
        + " отзывы" * 30,
        "Торговый центр",
        4.2,
    )

    assert cleaned.name == "Альянс"


def test_address_drops_leading_rubric_copies():
    address = clean_address(
        "Торговый центр, торговый центр, Кохомское ш., 4А, Иваново",
        "Торговый центр",
    )

    assert address == "Кохомское ш., 4А, Иваново"


def test_moved_status_and_narrow_space():
    moved = clean_place_name("Пицца МчитсяПереехало", "Пиццерия", None)
    hours = clean_place_name("Энгельс5Закрыто до 8:00\u202fAM", "Кофейня", 5)

    assert moved.name == "Пицца Мчится"
    assert moved.card_status == "Переехало"
    assert hours.name == "Энгельс"
    assert hours.opening_hours == "Закрыто до 8:00 AM"


def test_blacklist_wins_and_unknown_rubrics_are_dropped():
    assert classify_rubric("Лизинговая компания") == (None, "blacklist")
    assert classify_rubric("Продукты питания оптом") == (None, "blacklist")
    assert classify_rubric("Бухгалтерские услуги") == (None, "blacklist")
    assert classify_rubric("Аптека") == (None, "not_in_whitelist")
    assert classify_rubric("Магазин продуктов") == (None, "not_in_whitelist")
    assert classify_rubric("Столовая") == (None, "not_in_whitelist")
    assert classify_rubric(None) == (None, "missing_type")
    assert classify_rubric("Кофейня") == ("food", "kept")
    assert classify_rubric("Торговый центр") == ("mall", "kept")
    assert classify_rubric("Городской парк") == ("nature", "kept")
    assert classify_rubric("Сквер") == ("nature", "kept")
    assert classify_rubric("Музей") == ("culture", "kept")
    assert classify_rubric("Кинотеатр") == ("entertainment", "kept")
    assert classify_rubric("Вокзал") == ("station", "kept")
    assert classify_rubric("Бассейн") == ("sport", "kept")
    assert classify_rubric("Храм") == ("sight", "kept")


def test_loader_filters_records_and_keeps_reviews_out_of_metadata(tmp_path: Path):
    source = tmp_path / "places.json"
    source.write_text(
        json.dumps(
            [
                _record("1", "Кофейня", "Вафля House4,9Капучино: от 180 ₽", "Тихо и вкусно"),
                _record("1", "Кафе", "Повтор", "Другой отзыв"),
                _record("2", "Лизинговая компания", "Европлан4,4Закрыто до 9:00 AM", "Лизинг"),
                _record("3", "Аптека", "Аптека5", "Лекарства"),
                _record("4", "Кофейня", "Без координат", "Нет точки", coordinates=None),
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result = load_places(source)
    documents = places_to_documents(result.places)

    assert [place.org_id for place in result.places] == ["1"]
    assert result.places[0].name == "Вафля House"
    assert result.dropped["duplicate_org_id"] == 1
    assert result.dropped["blacklist"] == 1
    assert result.dropped["not_in_whitelist"] == 1
    assert result.dropped["invalid_coordinates"] == 1
    assert "comments" not in documents[0].metadata
    assert "Тихо и вкусно" in documents[0].page_content
    assert "Тихо и вкусно" not in json.dumps(documents[0].metadata, ensure_ascii=False)


def test_ivanovo_index_contains_only_whitelisted_categories():
    result = load_places(SOURCE)

    assert result.by_category.keys() <= set(CANONICAL_CATEGORIES)
    assert "nature" in result.by_category
    assert any(place.original_type == "Городской парк" for place in result.places)
    assert all(place.original_type != "Лизинговая компания" for place in result.places)
    assert all(place.original_type != "Аптека" for place in result.places)
    assert all("comments" not in place.metadata for place in result.places)
    assert any(place.review_excerpt and place.review_excerpt not in json.dumps(place.metadata, ensure_ascii=False) for place in result.places)


def _record(org_id, rubric, name, review, coordinates=...):
    if coordinates is ...:
        coordinates = {"lon": 40.97, "lat": 57.0}
    return {
        "org_id": org_id,
        "name": name,
        "type": rubric,
        "coordinates": coordinates,
        "rating": "4.9.",
        "reviews": "2",
        "address": "ул. Ленина, 30",
        "phone": "+7 000",
        "website": "https://example.test",
        "url": "https://example.test/org",
        "comments_truncated": False,
        "comments": [{"text": review}],
    }
