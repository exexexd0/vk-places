import asyncio
import json
import re
import ssl
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

from playwright.async_api import async_playwright

BASE_DIR = Path(__file__).resolve().parent

# Один запрос на картах обрезается. Ниже — рубрики, вместе они покрывают
# организации города. Узкие места («магазин», «кафе») при длинном списке
# дополнительно режутся сеткой по границам города.
RUBRICS = (
    "организация",
    "компания",
    "магазин",
    "услуги",
    "офис",
    "кафе",
    "ресторан",
    "бар",
    "столовая",
    "пиццерия",
    "суши",
    "кофейня",
    "пекарня",
    "фастфуд",
    "продукты",
    "супермаркет",
    "рынок",
    "аптека",
    "поликлиника",
    "больница",
    "стоматология",
    "медицинский центр",
    "ветеринарная клиника",
    "лаборатория анализов",
    "оптика",
    "парикмахерская",
    "салон красоты",
    "барбершоп",
    "автосервис",
    "автомойка",
    "шиномонтаж",
    "азс",
    "автосалон",
    "автозапчасти",
    "автошкола",
    "одежда",
    "обувь",
    "электроника",
    "мебель",
    "строительный магазин",
    "детские товары",
    "зоомагазин",
    "цветы",
    "ювелирный магазин",
    "книжный магазин",
    "спорттовары",
    "торговый центр",
    "банк",
    "банкомат",
    "почта",
    "нотариус",
    "юридические услуги",
    "бухгалтерские услуги",
    "страховая компания",
    "турагентство",
    "гостиница",
    "хостел",
    "фитнес клуб",
    "бассейн",
    "спортзал",
    "сауна",
    "химчистка",
    "ремонт телефонов",
    "ломбард",
    "школа",
    "детский сад",
    "детский центр",
    "университет",
    "курсы",
    "кинотеатр",
    "музей",
    "библиотека",
    "парк",
    "мфц",
    "управляющая компания",
    "церковь",
    "производство",
    "завод",
    "склад",
    "типография",
    "строительство",
    "ремонт квартир",
    "окна",
    "такси",
    "транспортная компания",
    "рекламное агентство",
    "охрана",
    "клининг",
    "фотосалон",
    "ателье",
    "коворкинг",
)

# Если в одной области собралось столько организаций, список, скорее всего,
# упёрся в потолок Яндекса — область делится на 4. Меньше порог — меньше
# шанс потерять хвост и больше запросов.
SATURATION = 200
MAX_SPLIT_DEPTH = 3
MIN_SPAN = 0.02
MAX_SCROLL_ROUNDS = 80
SCROLL_STALE_ROUNDS = 3
# Карточки отзывов Яндекс отдаёт страницами, прокрутка следующую порцию не добавляет.
# Четыре страницы — это около двухсот текстов. Дальше у очень больших мест список обрезается.
MAX_REVIEW_PAGES = 4
CARD_WORKERS = 4

ORG_HREF_RE = re.compile(r"/maps/org/(?:[^/?#]+/)?(\d+)")

EXTRACT_LINKS_JS = """
() => {
    const found = {};
    const re = /\\/maps\\/org\\/(?:[^/?#]+\\/)?(\\d+)/;
    for (const link of document.querySelectorAll('a[href*="/maps/org/"]')) {
        const href = link.href || link.getAttribute('href') || '';
        const match = href.match(re);
        if (!match) continue;
        found[match[1]] = 'https://yandex.ru/maps/org/' + match[1] + '/';
    }
    return found;
}
"""

SCROLL_LIST_JS = """
() => {
    const links = [...document.querySelectorAll('a[href*="/maps/org/"]')];
    const fallback = {x: 240, y: 480, moved: false};
    if (!links.length) return fallback;

    const last = links[links.length - 1];
    last.scrollIntoView({block: 'end'});

    let node = last.parentElement;
    let container = null;
    while (node && node !== document.body) {
        const style = getComputedStyle(node);
        const overflow = style.overflowY;
        if ((overflow === 'auto' || overflow === 'scroll' || overflow === 'overlay')
            && node.scrollHeight > node.clientHeight + 40) {
            container = node;
            break;
        }
        node = node.parentElement;
    }

    let moved = false;
    if (container) {
        const before = container.scrollTop;
        const step = Math.max(320, container.clientHeight * 0.75);
        container.scrollTop = Math.min(container.scrollTop + step, container.scrollHeight);
        moved = container.scrollTop !== before;
    }

    let panel = last;
    node = last.parentElement;
    while (node && node !== document.body) {
        const rect = node.getBoundingClientRect();
        if (rect.height > 320 && rect.width > 180 && rect.width < 700 && rect.left < 120) {
            panel = node;
        }
        node = node.parentElement;
    }
    const rect = panel.getBoundingClientRect();
    const x = Math.max(20, Math.min(rect.left + rect.width / 2, window.innerWidth - 20));
    const y = Math.max(20, Math.min(rect.top + rect.height * 0.65, window.innerHeight - 20));

    for (const el of document.querySelectorAll('button, a, span')) {
        const label = (el.innerText || '').trim().toLowerCase();
        if (label === 'показать ещё' || label === 'показать еще') {
            el.click();
            moved = true;
            break;
        }
    }
    return {x, y, moved};
}
"""

EXPAND_REVIEWS_JS = """
() => {
    for (const el of document.querySelectorAll('.business-review-view__expand, .spoiler-view__button')) {
        el.click();
    }
}
"""

EXTRACT_REVIEWS_JS = r"""
() => {
    const reviews = [];
    for (const node of document.querySelectorAll('.business-review-view')) {
        const textEl = node.querySelector(
            '.business-review-view__body, .business-review-view__body-text, [itemprop="reviewBody"]'
        );
        let text = textEl ? textEl.innerText.trim() : '';
        text = text.replace(/\s*(ещё|еще|more)\s*$/i, '').trim();
        if (!text) continue;
        const authorEl = node.querySelector('.business-review-view__author-name');
        const dateEl = node.querySelector('meta[itemprop="datePublished"]');
        const ratingEl = node.querySelector('meta[itemprop="ratingValue"]');
        reviews.push({
            author: authorEl ? authorEl.innerText.trim() : '',
            date: dateEl ? (dateEl.getAttribute('content') || '') : '',
            rating: ratingEl ? (ratingEl.getAttribute('content') || '') : '',
            text
        });
    }
    return reviews;
}
"""


def format_span(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}ч {minutes:02d}м"
    return f"{minutes}м {secs:02d}с"


def eta_line(started, done, total, label):
    if done <= 0 or total <= 0:
        return f"ETA {label}: ещё нет оценки"
    elapsed = time.monotonic() - started
    left = elapsed / done * max(total - done, 0)
    finish = time.strftime("%H:%M:%S", time.localtime(time.time() + left))
    return (
        f"ETA {label}: осталось {format_span(left)}, "
        f"конец около {finish}, прошло {format_span(elapsed)}"
    )


def city_slug(city_name):
    slug = city_name.strip().lower().replace(" ", "_")
    slug = re.sub(r"[^\w\-]+", "", slug, flags=re.UNICODE)
    return slug or "city"


def save_json(path, payload):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
    last_error = None
    for attempt in range(8):
        try:
            temporary.replace(path)
            return
        except PermissionError as error:
            last_error = error
            time.sleep(0.4 * (attempt + 1))
    raise PermissionError(f"Не удалось заменить {path.name}") from last_error


# Рамка, уже полученная для Иваново. Нужна, если Nominatim снова оборвёт SSL.
KNOWN_BOUNDS = {
    "иваново": (56.9466940, 57.0706995, 40.8678799, 41.1266349),
}


def fetch_city_bounds(city_name):
    """Границы населённого пункта из OpenStreetMap, чтобы резать карту сеткой."""
    query = urllib.parse.urlencode({
        "q": f"{city_name}, Россия",
        "format": "jsonv2",
        "limit": "5",
    })
    request = urllib.request.Request(
        f"https://nominatim.openstreetmap.org/search?{query}",
        headers={"User-Agent": "local-yandex-maps-parser/1.0"},
    )
    context = ssl.create_default_context()
    last_error = None
    places = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=30, context=context) as response:
                places = json.loads(response.read().decode())
            break
        except Exception as error:
            last_error = error
            print(f"Границы города, попытка {attempt + 1}/4 не удалась: {error}")
            time.sleep(2 * (attempt + 1))

    if places is None:
        cached = KNOWN_BOUNDS.get(city_name.strip().lower())
        if cached is None:
            raise RuntimeError(f"Не удалось найти границы города: {city_name}") from last_error
        south, north, west, east = cached
        pad = 0.004
        print(f"Беру сохранённую рамку для {city_name}")
        return south - pad, north + pad, west - pad, east + pad

    named = [
        place for place in places
        if place.get("display_name", "").lower().startswith(city_name.strip().lower())
    ]
    candidates = named or places
    if not candidates:
        raise RuntimeError(f"Не удалось найти границы города: {city_name}")

    def area(place):
        south, north, west, east = (float(value) for value in place["boundingbox"])
        return (north - south) * (east - west)

    place = max(candidates, key=area)
    south, north, west, east = (float(value) for value in place["boundingbox"])
    pad = 0.004
    print(f"Границы «{place.get('display_name', city_name)}»")
    return south - pad, north + pad, west - pad, east + pad


def split_bounds(bounds):
    south, north, west, east = bounds
    mid_lat = (south + north) / 2
    mid_lon = (west + east) / 2
    return (
        (south, mid_lat, west, mid_lon),
        (south, mid_lat, mid_lon, east),
        (mid_lat, north, west, mid_lon),
        (mid_lat, north, mid_lon, east),
    )


def can_split(bounds, depth):
    south, north, west, east = bounds
    if depth >= MAX_SPLIT_DEPTH:
        return False
    return (north - south) >= MIN_SPAN * 2 and (east - west) >= MIN_SPAN * 2


def query_key(rubric, bounds):
    south, north, west, east = bounds
    return f"{rubric}|{south:.4f}|{west:.4f}|{north:.4f}|{east:.4f}"


def build_search_url(rubric, bounds, city_name, depth):
    south, north, west, east = bounds
    lon = (west + east) / 2
    lat = (south + north) / 2
    span_lon = max(east - west, 0.002)
    span_lat = max(north - south, 0.002)
    # На всю городскую рамку добавляем имя города. Внутри клетки имя убираем,
    # иначе каждая клетка повторяет одну и ту же городскую верхушку выдачи.
    text = f"{rubric} {city_name}" if depth == 0 else rubric
    return (
        "https://yandex.ru/maps/?text="
        f"{urllib.parse.quote(text)}"
        f"&ll={lon:.6f},{lat:.6f}&spn={span_lon:.6f},{span_lat:.6f}"
    )


def reviews_page_url(url, page_index):
    base = (url or "").split("?")[0].rstrip("/")
    if not base.endswith("/reviews"):
        base += "/reviews"
    if page_index <= 1:
        return base + "/"
    return f"{base}/?page={page_index}"


def links_from_url(url):
    match = ORG_HREF_RE.search(url or "")
    if not match:
        return {}
    org_id = match.group(1)
    return {org_id: f"https://yandex.ru/maps/org/{org_id}/"}


def load_state(path):
    if not path.exists():
        return {"done": set(), "links": {}}
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    return {
        "done": set(payload.get("done", [])),
        "links": dict(payload.get("links", {})),
    }


def save_state(path, state):
    save_json(path, {
        "done": sorted(state["done"]),
        "links": state["links"],
    })


def load_records(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as file:
        payload = json.load(file)
    return payload if isinstance(payload, list) else []


async def dismiss_overlays(page):
    for label in ("Принять", "Хорошо", "Понятно", "Не сейчас", "Allow all"):
        button = page.get_by_role("button", name=label)
        try:
            if await button.count():
                await button.first.click(timeout=1000)
        except Exception:
            continue


async def wait_out_check(page):
    for _ in range(36):
        text = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 400)"
        )
        lowered = text.lower()
        if "не робот" in lowered or "smartcaptcha" in lowered or "подтвердите, что запросы" in lowered:
            print("В открытом окне проверка Яндекса. Пройдите её вручную, скрипт подождёт.")
            await page.wait_for_timeout(5000)
            continue
        return
    raise RuntimeError("Проверка Яндекса не исчезла. Пройдите её в окне браузера и запустите скрипт снова.")


async def open_maps(page, url):
    last_error = None
    for attempt in range(6):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(1200)
            status = response.status if response else 0
            snippet = await page.evaluate(
                "() => (document.body && document.body.innerText || '').trim().slice(0, 80)"
            )
        except Exception as error:
            last_error = error
            pause = 8 * (attempt + 1)
            print(f"Страница не открылась ({error}). Пауза {pause} с, попытка {attempt + 1}/6.")
            await asyncio.sleep(pause)
            continue
        if status == 429 or snippet.lower() == "limited":
            pause = 15 * (attempt + 1)
            print(f"Яндекс ограничил частоту запросов. Пауза {pause} с. Уже собранное лежит в файлах города.")
            await asyncio.sleep(pause)
            continue
        await dismiss_overlays(page)
        await wait_out_check(page)
        return
    raise RuntimeError(f"Страница так и не открылась: {last_error}")


async def scroll_results(page):
    point = await page.evaluate(SCROLL_LIST_JS)
    if point:
        await page.mouse.move(point["x"], point["y"])
        await page.mouse.wheel(0, 900)


async def collect_org_links(page):
    """Прокручивает боковой список, пока число организаций не перестанет расти."""
    await dismiss_overlays(page)
    try:
        await page.wait_for_selector('a[href*="/maps/org/"]', timeout=12000)
    except Exception:
        direct = links_from_url(page.url)
        if direct:
            return direct, False
        text = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 500)"
        )
        if "не найдено" in text.lower():
            return {}, False
        return {}, False

    found = {}
    stale_rounds = 0
    for round_index in range(MAX_SCROLL_ROUNDS):
        batch = await page.evaluate(EXTRACT_LINKS_JS)
        batch.update(links_from_url(page.url))
        before = len(found)
        found.update(batch)
        if len(found) == before:
            stale_rounds += 1
        else:
            stale_rounds = 0
        if round_index % 5 == 0:
            print(f"  прокрутка {round_index + 1}, в списке {len(found)}")
        if stale_rounds >= SCROLL_STALE_ROUNDS:
            return found, False
        await scroll_results(page)
        await page.wait_for_timeout(700)

    print(f"  список всё ещё растёт после {MAX_SCROLL_ROUNDS} прокруток")
    return found, True


async def search_area(page, rubric, bounds, depth, city_name, state, state_path):
    key = query_key(rubric, bounds)
    if key in state["done"]:
        return

    url = build_search_url(rubric, bounds, city_name, depth)
    print(f"Поиск [{depth}] {rubric}")
    await open_maps(page, url)
    found, hit_round_cap = await collect_org_links(page)

    added = 0
    for org_id, link in found.items():
        if org_id not in state["links"]:
            state["links"][org_id] = link
            added += 1
    print(
        f"  в этой выдаче {len(found)}, новых {added}, "
        f"всего уникальных {len(state['links'])}"
    )

    if not found:
        text = await page.evaluate(
            "() => (document.body && document.body.innerText || '').slice(0, 800)"
        )
        lowered = text.lower()
        empty_markers = ("не найдено", "ничего не нашлось", "ничего не нашли")
        if not any(marker in lowered for marker in empty_markers):
            print("  список организаций не появился, запрос повторю при следующем запуске")
            return

    state["done"].add(key)
    save_state(state_path, state)

    truncated = hit_round_cap or len(found) >= SATURATION
    if not truncated or not can_split(bounds, depth):
        await asyncio.sleep(1.0)
        return
    if depth > 0 and added == 0:
        print("  в этой клетке новых организаций нет, дальше не делю")
        return

    print("  выдача похожа на обрезанную, делю область на 4 части")
    for child in split_bounds(bounds):
        await asyncio.sleep(1.0)
        await search_area(page, rubric, child, depth + 1, city_name, state, state_path)


def write_readable(path, organizations):
    with path.open("w", encoding="utf-8") as file:
        file.write(f"Найдено организаций: {len(organizations)}\n\n")
        for index, org in enumerate(organizations, 1):
            file.write(f"{index}. {org.get('name', '')}\n")
            file.write(f"   Адрес: {org.get('address', '')}\n")
            file.write(f"   Рейтинг: {org.get('rating', '')} ({org.get('reviews', '')} оценок)\n")
            if org.get("phone"):
                file.write(f"   Телефон: {org['phone']}\n")
            if org.get("website"):
                file.write(f"   Сайт: {org['website']}\n")
            if org.get("url"):
                file.write(f"   Карточка: {org['url']}\n")
            comments = org.get("comments") or []
            if comments:
                file.write(f"   Отзывы ({len(comments)}):\n")
                for comment in comments:
                    author = comment.get("author") or "без имени"
                    text = (comment.get("text") or "").replace("\n", " ")
                    file.write(f"   - {author}: {text}\n")
            file.write("\n")


async def parse_yandex_maps_detailed(city_name):
    """Собирает организации города: рубрики, полная прокрутка, сетка, затем карточки."""
    slug = city_slug(city_name)
    state_path = BASE_DIR / f"{slug}_state.json"
    records_path = BASE_DIR / f"{slug}.json"
    final_path = BASE_DIR / f"{slug}_final.json"
    readable_path = BASE_DIR / f"{slug}_readable.txt"

    bounds = fetch_city_bounds(city_name)
    south, north, west, east = bounds
    print(f"Город: {city_name}")
    print(f"Рамка: {south:.4f}..{north:.4f}, {west:.4f}..{east:.4f}")
    print(f"Рубрик: {len(RUBRICS)}. Потолок одной выдачи: {SATURATION}.")

    state = load_state(state_path)
    records = load_records(records_path)
    saved_ids = {item["org_id"] for item in records if item.get("org_id")}

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(
            locale="ru-RU",
            viewport={"width": 1440, "height": 900},
        )
        page = await context.new_page()
        try:
            rubrics_started = time.monotonic()
            for index, rubric in enumerate(RUBRICS, 1):
                print(
                    f"\n=== Рубрика {index}/{len(RUBRICS)}: {rubric} | "
                    f"{eta_line(rubrics_started, index - 1, len(RUBRICS), 'рубрик')} ==="
                )
                for attempt in range(3):
                    try:
                        await search_area(page, rubric, bounds, 0, city_name, state, state_path)
                        break
                    except Exception as error:
                        pause = 10 * (attempt + 1)
                        print(f"Рубрика «{rubric}» прервана: {error}. Пауза {pause} с.")
                        await asyncio.sleep(pause)
                else:
                    print(f"Рубрика «{rubric}» пропущена, повтор будет при следующем запуске")
                print(eta_line(rubrics_started, index, len(RUBRICS), "рубрик"))

            pending = [org_id for org_id in state["links"] if org_id not in saved_ids]
            print(f"\nУникальных организаций: {len(state['links'])}")
            print(f"Карточек к открытию: {len(pending)}. Уже в файле: {len(saved_ids)}")

            cards_started = time.monotonic()
            for number, org_id in enumerate(pending, 1):
                url = state["links"][org_id]
                print(
                    f"Карточка {number}/{len(pending)} | "
                    f"{eta_line(cards_started, number - 1, len(pending), 'карточек')}: {url}"
                )
                try:
                    await open_maps(page, url)
                    org_data = await extract_organization_details(page, "")
                except Exception as error:
                    print(f"Ошибка карточки {org_id}: {error}")
                    print(eta_line(cards_started, number, len(pending), "карточек"))
                    continue

                org_data["org_id"] = org_id
                org_data["url"] = url
                name = (org_data.get("name") or "").strip()
                if not name or name == "Ошибка извлечения":
                    print(f"Пустая карточка {org_id}, повторю при следующем запуске")
                    print(eta_line(cards_started, number, len(pending), "карточек"))
                    continue

                if (org_data.get("reviews") or "") == "0":
                    org_data["comments"] = []
                else:
                    try:
                        org_data["comments"], truncated = await collect_comments(page, url)
                        org_data["comments_truncated"] = truncated
                    except Exception as error:
                        print(f"Отзывы {org_id} не собраны: {error}")
                        org_data["comments"] = []

                records.append(org_data)
                saved_ids.add(org_id)
                save_json(records_path, records)
                print(
                    f"Сохранено {len(records)} в {records_path.name}: {name} | "
                    f"комментариев {len(org_data.get('comments') or [])} | "
                    f"{eta_line(cards_started, number, len(pending), 'карточек')}"
                )
                await asyncio.sleep(1.5)

            save_json(final_path, records)
            write_readable(readable_path, records)
            print(f"\nГотово. Организаций: {len(records)}")
            print(f"Файлы: {records_path.name}, {final_path.name}, {readable_path.name}")
        except Exception as error:
            print(f"Произошла ошибка: {error}")
            try:
                await page.screenshot(path=str(BASE_DIR / "error_screenshot.png"))
            except Exception:
                pass
            if records:
                save_json(records_path, records)
                save_json(final_path, records)
                write_readable(readable_path, records)
            raise
        finally:
            await browser.close()
            print(f"Браузер закрыт для города: {city_name}")

async def extract_organization_details(page, card_text):
    """Извлекает детальную информацию об организации (УЛУЧШЕННАЯ ВЕРСИЯ)"""
    
    try:
        # Ждем загрузки детальной информации (увеличиваем время)
        await page.wait_for_timeout(5000)
        
        # Извлекаем данные с помощью JavaScript (улучшенная версия)
        org_data = await page.evaluate("""
            () => {
                const data = {
                    name: '',
                    rating: '',
                    reviews: '',
                    address: '',
                    phone: '',
                    website: ''
                };
                
                // Получаем весь текст страницы для поиска по содержимому
                const allText = document.body.textContent || document.body.innerText;
                const allHTML = document.body.innerHTML;
                
                // 1. ПОИСК НАЗВАНИЯ
                // Ищем h1 или заголовки
                const nameElements = document.querySelectorAll('h1, [class*="title"], [class*="name"]');
                for (const el of nameElements) {
                    if (el.textContent && el.textContent.trim().length > 2) {
                        data.name = el.textContent.trim();
                        break;
                    }
                }
                
                // 2. ПОИСК ТЕЛЕФОНА - УНИВЕРСАЛЬНЫЙ ПОДХОД
                // Паттерны для российских номеров
                const phonePatterns = [
                    /\\+7\\s*\\(?\\d{3}\\)?[\\s\\-]?\\d{3}[\\s\\-]?\\d{2}[\\s\\-]?\\d{2}/g,
                    /8\\s*\\(?\\d{3}\\)?[\\s\\-]?\\d{3}[\\s\\-]?\\d{2}[\\s\\-]?\\d{2}/g,
                    /\\+7[\\s\\-]?\\d{10}/g,
                    /8[\\s\\-]?\\d{10}/g
                ];
                
                for (const pattern of phonePatterns) {
                    const matches = allText.match(pattern);
                    if (matches && matches.length > 0) {
                        // Берем первый найденный номер
                        data.phone = matches[0].trim();
                        break;
                    }
                }
                
                // 3. ПОИСК САЙТА - ИЩЕМ ВСЕ ССЫЛКИ НА ВНЕШНИЕ ДОМЕНЫ
                const links = document.querySelectorAll('a[href]');
                for (const link of links) {
                    const href = link.href;
                    if (href && 
                        (href.startsWith('http://') || href.startsWith('https://')) &&
                        !href.includes('yandex.ru') && 
                        !href.includes('maps.yandex') &&
                        !href.includes('tel:') && 
                        !href.includes('mailto:') &&
                        !href.includes('google.com') &&
                        !href.includes('vk.com') &&
                        !href.includes('instagram.com') &&
                        !href.includes('facebook.com') &&
                        !href.includes('twitter.com') &&
                        !href.includes('telegram.me') &&
                        !href.includes('t.me') &&
                        !href.includes('whatsapp.com') &&
                        !href.includes('viber.com')) {
                        
                        // Извлекаем домен из URL
                        try {
                            const url = new URL(href);
                            data.website = url.hostname.replace('www.', '');
                            break;
                        } catch(e) {
                            data.website = href;
                            break;
                        }
                    }
                }
                
                // Дополнительный поиск сайта по тексту
                if (!data.website) {
                    const websitePatterns = [
                        /[a-zA-Z0-9-]+\\.(ru|com|org|net|info|biz|рф)\\b/g,
                        /www\\.[a-zA-Z0-9-]+\\.[a-zA-Z]{2,}/g
                    ];
                    
                    for (const pattern of websitePatterns) {
                        const matches = allText.match(pattern);
                        if (matches && matches.length > 0) {
                            for (const match of matches) {
                                if (!match.includes('yandex') && !match.includes('maps') && !match.includes('google')) {
                                    data.website = match.replace('www.', '');
                                    break;
                                }
                            }
                            if (data.website) break;
                        }
                    }
                }
                
                // 4. ПОИСК ОТЗЫВОВ - ИЩЕМ ЧИСЛА ПЕРЕД СЛОВОМ "ОТЗЫВ"
                const reviewPatterns = [
                    /(\\d+)\\s*отзыв/gi,
                    /(\\d+)\\s*оценк/gi,
                    /(\\d+)\\s*review/gi
                ];
                
                for (const pattern of reviewPatterns) {
                    const matches = allText.match(pattern);
                    if (matches && matches.length > 0) {
                        const reviewMatch = matches[0].match(/\\d+/);
                        if (reviewMatch) {
                            data.reviews = reviewMatch[0];
                            break;
                        }
                    }
                }
                
                // 5. ПОИСК РЕЙТИНГА
                const ratingPatterns = [
                    /рейтинг[\\s:]*([\\d,\\.]+)/gi,
                    /([\\d,\\.]+)\\s*из\\s*5/gi,
                    /([\\d,\\.]+)\\s*★/gi
                ];
                
                for (const pattern of ratingPatterns) {
                    const matches = allText.match(pattern);
                    if (matches && matches.length > 0) {
                        const ratingMatch = matches[0].match(/[\\d,\\.]+/);
                        if (ratingMatch) {
                            data.rating = ratingMatch[0].replace(',', '.');
                            break;
                        }
                    }
                }
                
                // 6. ПОИСК АДРЕСА
                const addressPatterns = [
                    /(ул\\.|улица|пр\\.|проспект|пер\\.|переулок|бул\\.|бульвар|наб\\.|набережная|ш\\.|шоссе)\\s+[А-Яа-яё\\s\\-,\\.\\d]+/gi,
                    /[А-Яа-яё\\s]+ул\\.,?\\s*[А-Яа-яё\\s\\-\\d,]+/gi,
                    /[А-Яа-яё\\s,\\-\\.\\d]+,\\s*(Липецк|Тамбов|Ульяновск|Самара|Пермь|Казань|Брянск|Белгород|Владимир|Воронеж|Иваново|Димитровград|Уфа|Екатеринбург)/gi
                ];
                
                for (const pattern of addressPatterns) {
                    const matches = allText.match(pattern);
                    if (matches && matches.length > 0) {
                        const address = matches[0].trim();
                        if (address.length > 10 && /\\d/.test(address)) {
                            data.address = address;
                            break;
                        }
                    }
                }
                
                // Логируем результаты для отладки
                console.log('Extracted data:', data);
                
                return data;
            }
        """)
        
        # Очищаем и обрабатываем данные
        name = org_data.get('name', '').strip()
        if not name and card_text:
            # Пытаемся извлечь название из карточки
            name_match = re.search(r'^([^•\n]+)', card_text)
            name = name_match.group(1).strip() if name_match else "Нет названия"
        
        rating = org_data.get('rating', '').strip()
        if not rating and card_text:
            rating_match = re.search(r'(\d+\.?\d*)', card_text)
            rating = rating_match.group(1) if rating_match else "Нет рейтинга"
        
        reviews = org_data.get('reviews', '').strip()
        if not reviews and card_text:
            reviews_match = re.search(r'(\d+)\s*оценок?', card_text)
            reviews = reviews_match.group(1) if reviews_match else "0"
        
        address = org_data.get('address', '').strip()
        phone = org_data.get('phone', '').strip()
        website = org_data.get('website', '').strip()
        
        # Дополнительная очистка данных
        if phone:
            # Нормализуем номер телефона
            phone = re.sub(r'[^\d\+\(\)\s\-]', '', phone)
            phone = phone.strip()
        
        if website:
            # Очищаем website от лишних символов
            website = website.replace('http://', '').replace('https://', '').replace('www.', '')
            website = website.split('/')[0]  # Убираем путь, оставляем только домен
        
        return {
            'name': name,
            'rating': rating,
            'reviews': reviews,
            'address': address,
            'phone': phone,
            'website': website
        }
        
    except Exception as e:
        print(f"Ошибка при извлечении данных: {e}")
        return {
            'name': "Ошибка извлечения",
            'rating': "Нет данных",
            'reviews': "0",
            'address': "Нет данных",
            'phone': "Нет данных",
            'website': "Нет данных"
        }


async def collect_comments(page, org_url):
    """Открывает вкладку отзывов и забирает тексты со страниц выдачи."""
    collected = []
    seen = set()
    truncated = False
    for page_index in range(1, MAX_REVIEW_PAGES + 1):
        await open_maps(page, reviews_page_url(org_url, page_index))
        try:
            await page.wait_for_selector(".business-review-view", timeout=8000)
        except Exception:
            text = await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 800)"
            )
            lowered = text.lower()
            empty_markers = (
                "отзывов пока нет",
                "нет отзывов",
                "ещё нет отзывов",
                "еще нет отзывов",
                "no reviews",
            )
            if any(marker in lowered for marker in empty_markers):
                break
            if page_index == 1 and not collected:
                raise RuntimeError("страница отзывов не загрузилась")
            break

        await page.evaluate(EXPAND_REVIEWS_JS)
        await page.wait_for_timeout(300)
        batch = await page.evaluate(EXTRACT_REVIEWS_JS)
        added = 0
        for item in batch:
            text = (item.get("text") or "").strip()
            if not text:
                continue
            key = ((item.get("author") or "").strip(), text)
            if key in seen:
                continue
            seen.add(key)
            collected.append({
                "author": (item.get("author") or "").strip(),
                "date": (item.get("date") or "").strip(),
                "rating": (item.get("rating") or "").strip(),
                "text": text,
            })
            added += 1
        if added == 0:
            break

        has_next = await page.evaluate(
            """(pageNo) => {
                const links = document.querySelectorAll('.seo-pagination-view a');
                for (const link of links) {
                    const href = link.getAttribute('href') || '';
                    if (href.includes('page=' + pageNo)) return true;
                }
                return false;
            }""",
            page_index + 1,
        )
        if not has_next:
            break
        if page_index == MAX_REVIEW_PAGES:
            truncated = True
    return collected, truncated


async def parse_review_texts(city_name, workers=CARD_WORKERS):
    """Собирает тексты отзывов в отдельный файл, не трогая текущий прогон карточек."""
    slug = city_slug(city_name)
    state_path = BASE_DIR / f"{slug}_state.json"
    records_path = BASE_DIR / f"{slug}.json"
    comments_path = BASE_DIR / f"{slug}_comments.json"

    state = load_state(state_path)
    if not state["links"]:
        print(f"Нет списка организаций в {state_path.name}. Сначала нужен основной прогон.")
        return

    hints = {}
    try:
        for item in load_records(records_path):
            org_id = str(item.get("org_id") or "")
            if not org_id:
                continue
            hints[org_id] = {
                "name": item.get("name") or "",
                "reviews": str(item.get("reviews") or ""),
            }
    except json.JSONDecodeError:
        print("Файл карточек сейчас перезаписывается, имена возьму из уже прочитанного списка.")

    saved = load_records(comments_path)
    done_ids = {str(item.get("org_id")) for item in saved if item.get("org_id")}

    pending = []
    skipped_empty = 0
    for org_id in state["links"]:
        if org_id in done_ids:
            continue
        hint = hints.get(org_id)
        if hint and hint["reviews"] == "0":
            skipped_empty += 1
            continue
        pending.append(org_id)

    print(f"Город: {city_name}")
    print(f"Организаций в списке: {len(state['links'])}")
    print(f"Уже с текстами отзывов: {len(done_ids)}")
    print(f"Без отзывов, пропускаю: {skipped_empty}")
    print(f"К открытию: {len(pending)}. Окон браузера: {workers}")
    print(f"Тексты пишу в {comments_path.name}. Текущий сбор карточек не трогаю.")
    if not pending:
        print("Собирать нечего.")
        return

    queue = asyncio.Queue()
    for org_id in pending:
        queue.put_nowait(org_id)

    lock = asyncio.Lock()
    stats = {"done": 0, "with_text": 0}
    started = time.monotonic()
    total = len(pending)

    async with async_playwright() as playwright:
        browsers = []
        pages = []
        try:
            for index in range(workers):
                x = (index % 2) * 980
                y = 0 if index < 2 else 420
                browser = await playwright.chromium.launch(
                    headless=False,
                    args=[
                        f"--window-position={x},{y}",
                        "--window-size=960,700",
                    ],
                )
                context = await browser.new_context(
                    locale="ru-RU",
                    viewport={"width": 960, "height": 640},
                )
                page = await context.new_page()
                browsers.append(browser)
                pages.append(page)
                print(f"Окно {index + 1}/{workers} открыто")

            async def worker(index, page):
                while True:
                    try:
                        org_id = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    url = state["links"][org_id]
                    hint = hints.get(org_id, {})
                    try:
                        comments, truncated = await collect_comments(page, url)
                    except Exception as error:
                        print(f"[{index + 1}] отзывы {org_id} не собраны: {error}")
                        continue
                    record = {
                        "org_id": org_id,
                        "url": url,
                        "name": hint.get("name", ""),
                        "comments": comments,
                        "comments_truncated": truncated,
                    }
                    async with lock:
                        saved.append(record)
                        save_json(comments_path, saved)
                        stats["done"] += 1
                        if comments:
                            stats["with_text"] += 1
                        done = stats["done"]
                        sample = ""
                        if comments:
                            sample = " | " + comments[0]["text"][:70].replace("\n", " ")
                        name = hint.get("name") or org_id
                        print(
                            f"[{index + 1}] {done}/{total} {name}: "
                            f"комментариев {len(comments)}{sample} | "
                            f"{eta_line(started, done, total, 'отзывов')}"
                        )
                    await asyncio.sleep(0.6)

            await asyncio.gather(*(worker(index, page) for index, page in enumerate(pages)))
            print(
                f"Готово. Просмотрено организаций: {stats['done']}. "
                f"С текстами: {stats['with_text']}. Файл: {comments_path.name}"
            )
        finally:
            for browser in browsers:
                await browser.close()


async def process_review_cities():
    cities = read_cities_from_file()
    if not cities:
        print("Список городов пуст. Добавьте названия в cities.txt, по одному на строку.")
        return
    for city in cities:
        await parse_review_texts(city, workers=CARD_WORKERS)


def read_cities_from_file(filename=None):
    """Читает список городов из файла"""
    path = Path(filename) if filename else BASE_DIR / "cities.txt"
    try:
        with path.open("r", encoding="utf-8") as file:
            return [line.strip() for line in file if line.strip()]
    except FileNotFoundError:
        print(f"Файл {path} не найден.")
        return []
    except Exception as error:
        print(f"Ошибка при чтении файла {path}: {error}")
        return []


async def process_all_cities():
    """Обрабатывает все города из файла cities.txt"""
    cities = read_cities_from_file()

    if not cities:
        print("Список городов пуст. Добавьте названия в cities.txt, по одному на строку.")
        return

    print(f"Найдено {len(cities)} городов для обработки:")
    for index, city in enumerate(cities, 1):
        print(f"{index}. {city}")

    print("\nНачинаем обработку городов...")

    for index, city in enumerate(cities, 1):
        print(f"\n{'=' * 50}")
        print(f"ОБРАБОТКА ГОРОДА {index}/{len(cities)}: {city}")
        print(f"{'=' * 50}")

        try:
            await parse_yandex_maps_detailed(city)
            print(f"Город {city} обработан.")
        except Exception as error:
            print(f"Ошибка при обработке города {city}: {error}")
            print("Продолжаем со следующим городом...")

        if index < len(cities):
            print("Пауза 10 секунд перед следующим городом...")
            await asyncio.sleep(10)


if __name__ == "__main__":
    if "--reviews" in sys.argv:
        asyncio.run(process_review_cities())
    else:
        asyncio.run(process_all_cities())
