import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from rag.categories import CATEGORY_LABELS, classify_rubric

PRICE_TAIL = re.compile(
    r"(?:Капучино|Ср\.\s*чек|Пиво|Роллы|Билет)\s*:\s*(?:от|до)\s+[\d\s\u00a0\u202f]+₽\s*$",
    re.IGNORECASE,
)
HOURS_TAIL = re.compile(
    r"(?:(?:Закрыто|Открыто)\s+до|До)\s+\d{1,2}:\d{2}\s*(?:AM|PM)?\s*$"
    r"|(?:Закрыто|Открыто)\s+до\s+[^\d\s]\S*\s*$"
    r"|24\s*ч\s*$"
    r"|Круглосуточно\s*$",
    re.IGNORECASE,
)
PAGE_TITLE = re.compile(r"Торговые центры(.+?)Рейтинг", re.DOTALL)
MOVED_TAIL = re.compile(r"Переехало\s*$", re.IGNORECASE)
CLOSED_TAIL = re.compile(r"Закрыто\s*$", re.IGNORECASE)
RATING_PATTERN = re.compile(r"\d+(?:[.,]\d+)?")
COUNT_PATTERN = re.compile(r"\d+")
REVIEW_LIMIT = 1000


@dataclass(frozen=True)
class CleanedName:
    name: str
    opening_hours: str | None
    card_status: str | None


@dataclass(frozen=True)
class Place:
    org_id: str
    name: str
    raw_name: str
    original_type: str
    category: str
    address: str
    rating: float | None
    reviews: int | None
    phone: str | None
    website: str | None
    url: str | None
    comments_truncated: bool
    city: str
    lat: float
    lon: float
    opening_hours: str | None
    card_status: str | None
    review_excerpt: str

    @property
    def page_content(self) -> str:
        lines = [
            self.name,
            f"Категория: {CATEGORY_LABELS[self.category]}",
            f"Рубрика: {self.original_type}",
            f"Адрес: {self.address}",
        ]
        if self.review_excerpt:
            lines.append(f"Отзывы: {self.review_excerpt}")
        return "\n".join(lines)

    @property
    def metadata(self) -> dict:
        return {
            "org_id": self.org_id,
            "name": self.name,
            "raw_name": self.raw_name,
            "type": self.original_type,
            "category": self.category,
            "address": self.address,
            "rating": self.rating,
            "reviews": self.reviews,
            "phone": self.phone,
            "website": self.website,
            "url": self.url,
            "comments_truncated": self.comments_truncated,
            "city": self.city,
            "lat": self.lat,
            "lon": self.lon,
            "opening_hours": self.opening_hours,
            "card_status": self.card_status,
            "coordinates": {"lon": self.lon, "lat": self.lat},
        }


@dataclass(frozen=True)
class FilterResult:
    places: list[Place]
    source_count: int
    dropped: dict[str, int]
    by_category: dict[str, int]


def clean_place_name(raw_name: str, rubric: str | None, rating: float | None) -> CleanedName:
    text = " ".join(str(raw_name or "").split())
    page_title = PAGE_TITLE.search(text)
    if page_title and len(text) > 80:
        text = " ".join(page_title.group(1).split())
    rubric_text = " ".join(str(rubric or "").split())
    opening_hours = None
    card_status = None
    for _ in range(20):
        peeled = _peel_name_tail(text, rubric_text, rating)
        if peeled is None:
            break
        text, kind, payload = peeled
        if kind == "hours":
            opening_hours = payload
        elif kind == "moved":
            card_status = "Переехало"
        elif kind == "closed":
            card_status = "Закрыто"
        text = text.strip(" -–—,")
        if not text:
            break
    return CleanedName(text or str(raw_name).strip(), opening_hours, card_status)


def clean_address(raw_address: str, rubric: str | None) -> str:
    text = " ".join(str(raw_address or "").split())
    rubric_text = " ".join(str(rubric or "").split())
    if not text or not rubric_text:
        return text
    parts = [part.strip() for part in text.split(",") if part.strip()]
    while parts and parts[0].casefold() == rubric_text.casefold():
        parts.pop(0)
    return ", ".join(parts)


def parse_rating(value) -> float | None:
    if value is None or value == "":
        return None
    match = RATING_PATTERN.search(str(value))
    if match is None:
        return None
    return float(match.group(0).replace(",", "."))


def parse_count(value) -> int | None:
    if value is None or value == "":
        return None
    match = COUNT_PATTERN.search(str(value))
    if match is None:
        return None
    return int(match.group(0))


def valid_coordinates(value) -> tuple[float, float] | None:
    if not isinstance(value, dict):
        return None
    try:
        lon = float(value["lon"])
        lat = float(value["lat"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (27 <= lon <= 60 and 50 <= lat <= 70):
        return None
    return lon, lat


def load_places(path: Path, city: str = "Иваново") -> FilterResult:
    records = json.loads(path.read_text(encoding="utf-8"))
    dropped: Counter[str] = Counter()
    places: list[Place] = []
    seen: set[str] = set()
    for record in records:
        org_id = str(record.get("org_id") or "").strip()
        if not org_id:
            dropped["missing_org_id"] += 1
            continue
        if org_id in seen:
            dropped["duplicate_org_id"] += 1
            continue
        seen.add(org_id)
        category, reason = classify_rubric(record.get("type"))
        if category is None:
            dropped[reason] += 1
            continue
        coordinates = valid_coordinates(record.get("coordinates"))
        if coordinates is None:
            dropped["invalid_coordinates"] += 1
            continue
        rating = parse_rating(record.get("rating"))
        rubric = str(record.get("type")).strip()
        cleaned = clean_place_name(str(record.get("name") or ""), rubric, rating)
        if not cleaned.name:
            dropped["empty_name"] += 1
            continue
        lon, lat = coordinates
        places.append(
            Place(
                org_id=org_id,
                name=cleaned.name,
                raw_name=str(record.get("name") or ""),
                original_type=rubric,
                category=category,
                address=clean_address(str(record.get("address") or ""), rubric),
                rating=rating,
                reviews=parse_count(record.get("reviews")),
                phone=_optional_text(record.get("phone")),
                website=_optional_text(record.get("website")),
                url=_optional_text(record.get("url")),
                comments_truncated=bool(record.get("comments_truncated")),
                city=city,
                lat=lat,
                lon=lon,
                opening_hours=cleaned.opening_hours,
                card_status=cleaned.card_status,
                review_excerpt=_review_excerpt(record.get("comments")),
            )
        )
    by_category = Counter(place.category for place in places)
    return FilterResult(
        places=places,
        source_count=len(records),
        dropped=dict(dropped),
        by_category=dict(by_category),
    )


def _peel_name_tail(
    text: str, rubric: str, rating: float | None
) -> tuple[str, str, str | None] | None:
    hours_match = HOURS_TAIL.search(text)
    if hours_match and hours_match.start() > 0:
        return text[: hours_match.start()], "hours", " ".join(hours_match.group(0).split())
    price_match = PRICE_TAIL.search(text)
    if price_match and price_match.start() > 0:
        return text[: price_match.start()], "price", None
    moved_match = MOVED_TAIL.search(text)
    if moved_match and moved_match.start() > 0:
        return text[: moved_match.start()], "moved", None
    closed_match = CLOSED_TAIL.search(text)
    if closed_match and closed_match.start() > 0:
        return text[: closed_match.start()], "closed", None
    if rubric and text.endswith(rubric) and len(text) > len(rubric):
        return text[: -len(rubric)], "rubric", None
    without_rating = _strip_glued_rating(text, rating)
    if without_rating != text:
        return without_rating, "rating", None
    return None


def _strip_glued_rating(text: str, rating: float | None) -> str:
    if rating is None:
        return text.strip()
    suffixes = []
    if abs(rating - round(rating)) < 1e-9:
        suffixes.append(str(int(round(rating))))
    else:
        decimal = f"{rating:.1f}"
        suffixes.extend((decimal.replace(".", ","), decimal))
    for suffix in suffixes:
        if text.endswith(suffix) and len(text) > len(suffix):
            return text[: -len(suffix)].strip()
    return text.strip()


def _review_excerpt(comments) -> str:
    parts: list[str] = []
    total = 0
    for comment in comments or []:
        text = " ".join(str((comment or {}).get("text") or "").split())
        if not text:
            continue
        remaining = REVIEW_LIMIT - total
        if remaining <= 0:
            break
        parts.append(text[:remaining])
        total += len(parts[-1]) + 1
    return " ".join(parts).strip()


def _optional_text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
