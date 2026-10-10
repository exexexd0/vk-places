import json
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CARDS_PATH = BASE_DIR / "иваново.json"
COMMENTS_PATH = BASE_DIR / "иваново_comments.json"
OUTPUT_PATH = BASE_DIR.parent / "Иваново.json"


def load_json_list(path):
    """Файлы в это время дописывает парсер, поэтому чтение повторяется."""
    last_error = None
    for attempt in range(8):
        try:
            with path.open(encoding="utf-8") as file:
                payload = json.load(file)
        except (json.JSONDecodeError, OSError) as error:
            last_error = error
            time.sleep(0.5 * (attempt + 1))
            continue
        if not isinstance(payload, list):
            raise RuntimeError(f"{path.name} должен быть списком")
        return payload
    raise RuntimeError(f"Не удалось прочитать {path.name}") from last_error


def save_json(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    temporary.replace(path)


def assemble(cards, comments):
    comments_by_id = {}
    for item in comments:
        org_id = str(item.get("org_id") or "")
        if org_id:
            comments_by_id[org_id] = item

    merged = []
    seen = set()
    matched = 0
    for org in cards:
        org_id = str(org.get("org_id") or "")
        extra = comments_by_id.get(org_id)
        if extra:
            matched += 1
            seen.add(org_id)
        comment_items = (extra or {}).get("comments") or org.get("comments") or []
        merged.append({
            "org_id": org_id,
            "name": org.get("name") or (extra or {}).get("name") or "",
            "type": None,
            "coordinates": None,
            "rating": org.get("rating") or "",
            "reviews": org.get("reviews") or "",
            "address": org.get("address") or "",
            "phone": org.get("phone") or "",
            "website": org.get("website") or "",
            "url": org.get("url") or (extra or {}).get("url") or "",
            "comments": comment_items,
            "comments_truncated": bool((extra or {}).get("comments_truncated")),
        })

    only_comments = 0
    for org_id, extra in comments_by_id.items():
        if org_id in seen:
            continue
        only_comments += 1
        merged.append({
            "org_id": org_id,
            "name": extra.get("name") or "",
            "type": None,
            "coordinates": None,
            "rating": "",
            "reviews": "",
            "address": "",
            "phone": "",
            "website": "",
            "url": extra.get("url") or "",
            "comments": extra.get("comments") or [],
            "comments_truncated": bool(extra.get("comments_truncated")),
        })

    with_text = sum(1 for item in merged if item.get("comments"))
    text_count = sum(len(item.get("comments") or []) for item in merged)
    return merged, matched, only_comments, with_text, text_count


def main():
    cards = load_json_list(CARDS_PATH)
    comments = load_json_list(COMMENTS_PATH)
    merged, matched, only_comments, with_text, text_count = assemble(cards, comments)
    save_json(OUTPUT_PATH, merged)
    print(f"Карточек: {len(cards)}")
    print(f"Организаций в отзывах: {len(comments)}")
    print(f"Совпало по org_id: {matched}")
    print(f"Только отзывы, карточки ещё нет: {only_comments}")
    print(f"В итоге: {len(merged)}")
    print(f"С текстами: {with_text}, комментариев: {text_count}")
    print(f"Файл: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
