import asyncio
import json
import re
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

from parser import dismiss_overlays, save_json, wait_out_check

try:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
except Exception:
    pass

DATA_PATH = Path(r"C:\анечкин проект\Иваново.json")
PROGRESS_PATH = DATA_PATH.with_name("Иваново_geo.jsonl")
WORKERS = 10
SAVE_EVERY = 20

SKIP_TYPES = {
    "карты",
    "maps",
    "иваново",
    "ivanovo",
    "россия",
    "russia",
}

OID_THEN_COORDS = re.compile(
    r"oid=(\d+).{0,700}?displayCoordinates\\?\"\s*:\s*\[(\d+\.\d+)\s*,\s*(\d+\.\d+)\]",
    re.S,
)
COORDS_THEN_OID = re.compile(
    r"displayCoordinates\\?\"\s*:\s*\[(\d+\.\d+)\s*,\s*(\d+\.\d+)\].{0,700}?oid=(\d+)",
    re.S,
)
ACTIVE_PIN = re.compile(
    r'data-mode="active"[^>]*data-coordinates="(\d+\.\d+),(\d+\.\d+)"'
)
LL_PARAM = re.compile(r"[?&]ll=(\d+\.\d+)%2C(\d+\.\d+)|[?&]ll=(\d+\.\d+),(\d+\.\d+)")

TYPE_JS = """
() => {
    const read = (selector) => [...document.querySelectorAll(selector)]
        .map(el => (el.innerText || '').replace(/\\s+/g, ' ').trim())
        .filter(Boolean);
    const primary = read('.orgpage-categories-info-view__link');
    if (primary.length) return primary;
    return read('.breadcrumbs-view__breadcrumb, .business-categories-view__category');
}
"""


def plausible(lon, lat):
    return 27 <= lon <= 60 and 50 <= lat <= 70


def pair_from_match(lon_text, lat_text):
    lon = float(lon_text)
    lat = float(lat_text)
    if plausible(lon, lat):
        return {"lon": round(lon, 6), "lat": round(lat, 6)}
    return None


def coordinates_from_html(html, org_id):
    for match in COORDS_THEN_OID.finditer(html):
        if match.group(3) == org_id:
            found = pair_from_match(match.group(1), match.group(2))
            if found:
                return found
    for match in OID_THEN_COORDS.finditer(html):
        if match.group(1) == org_id:
            found = pair_from_match(match.group(2), match.group(3))
            if found:
                return found
    pin = ACTIVE_PIN.search(html)
    if pin:
        return pair_from_match(pin.group(1), pin.group(2))
    return None


def coordinates_from_url(url):
    match = LL_PARAM.search(url or "")
    if not match:
        return None
    if match.group(1):
        return pair_from_match(match.group(1), match.group(2))
    return pair_from_match(match.group(3), match.group(4))


def choose_type(names):
    for name in names:
        if name.lower() not in SKIP_TYPES:
            return name
    return None


def load_records():
    with DATA_PATH.open(encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, list):
        raise RuntimeError("Иваново.json должен быть списком")
    return payload


def apply_progress(records):
    if not PROGRESS_PATH.exists():
        return
    by_id = {str(item.get("org_id") or ""): item for item in records}
    with PROGRESS_PATH.open(encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            record = by_id.get(str(item.get("org_id") or ""))
            if not record:
                continue
            if item.get("coordinates"):
                record["coordinates"] = item["coordinates"]
            if item.get("type"):
                record["type"] = item["type"]


def needs_fetch(record):
    return not record.get("coordinates")


async def drop_heavy(route):
    if route.request.resource_type in ("image", "media", "font"):
        await route.abort()
    else:
        await route.continue_()


async def open_org(page, url):
    last_error = None
    for attempt in range(4):
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=35000)
            status = response.status if response else 0
        except Exception as error:
            last_error = error
            pause = 3 * (attempt + 1)
            print(f"Страница не открылась ({error}). Пауза {pause} с, попытка {attempt + 1}/4.")
            await asyncio.sleep(pause)
            continue
        await dismiss_overlays(page)
        await wait_out_check(page)
        if status == 429:
            pause = 8 * (attempt + 1)
            print(f"Яндекс ограничил частоту. Пауза {pause} с.")
            await asyncio.sleep(pause)
            continue
        return
    raise RuntimeError(f"Страница так и не открылась: {last_error}")


async def read_org(page, org_id):
    try:
        await page.wait_for_selector(
            ".orgpage-categories-info-view__link, h1",
            timeout=6000,
        )
    except Exception:
        pass
    html = await page.content()
    coordinates = coordinates_from_html(html, org_id) or coordinates_from_url(page.url)
    names = await page.evaluate(TYPE_JS)
    return coordinates, choose_type(names or [])


def format_span(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}ч {minutes:02d}м"
    return f"{minutes}м {secs:02d}с"


async def run(workers):
    records = load_records()
    apply_progress(records)
    pending = [record for record in records if record.get("org_id") and record.get("url") and needs_fetch(record)]
    print(f"Организаций в файле: {len(records)}")
    print(f"Уже с координатами: {len(records) - len(pending)}")
    print(f"К открытию: {len(pending)}. Окон: {workers}")
    if not pending:
        save_json(DATA_PATH, records)
        print("Дописывать нечего.")
        return

    queue = asyncio.Queue()
    for record in pending:
        queue.put_nowait(record)

    lock = asyncio.Lock()
    stats = {"done": 0, "filled": 0}
    started = time.monotonic()
    total = len(pending)

    async with async_playwright() as playwright:
        browsers = []
        pages = []
        try:
            for index in range(workers):
                x = (index % 5) * 370
                y = 0 if index < 5 else 500
                browser = await playwright.chromium.launch(
                    headless=False,
                    args=[
                        f"--window-position={x},{y}",
                        "--window-size=370,480",
                    ],
                )
                context = await browser.new_context(
                    locale="ru-RU",
                    viewport={"width": 360, "height": 420},
                )
                await context.route("**/*", drop_heavy)
                page = await context.new_page()
                browsers.append(browser)
                pages.append(page)
                print(f"Окно {index + 1}/{workers} открыто")
                await asyncio.sleep(0.4)

            async def worker(index, page):
                await asyncio.sleep(index * 0.7)
                while True:
                    try:
                        record = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    org_id = str(record.get("org_id"))
                    url = record.get("url")
                    try:
                        await open_org(page, url)
                        coordinates, org_type = await read_org(page, org_id)
                    except Exception as error:
                        print(f"[{index + 1}] {org_id} не собран: {error}")
                        continue
                    if not coordinates and not org_type:
                        print(f"[{index + 1}] {org_id}: на странице нет координат и типа")
                        continue
                    async with lock:
                        if coordinates:
                            record["coordinates"] = coordinates
                        if org_type:
                            record["type"] = org_type
                        with PROGRESS_PATH.open("a", encoding="utf-8") as progress:
                            progress.write(json.dumps({
                                "org_id": org_id,
                                "type": record.get("type"),
                                "coordinates": record.get("coordinates"),
                            }, ensure_ascii=False) + "\n")
                        stats["done"] += 1
                        if coordinates and org_type:
                            stats["filled"] += 1
                        done = stats["done"]
                        if done % SAVE_EVERY == 0:
                            save_json(DATA_PATH, records)
                        elapsed = time.monotonic() - started
                        left = elapsed / done * max(total - done, 0)
                        place = (record.get("name") or org_id)[:40]
                        point = ""
                        if coordinates:
                            point = f"{coordinates['lon']}, {coordinates['lat']}"
                        print(
                            f"[{index + 1}] {done}/{total} {place}: "
                            f"{org_type or 'тип не найден'} | {point or 'координаты не найдены'} | "
                            f"осталось {format_span(left)}"
                        )

            await asyncio.gather(*(worker(index, page) for index, page in enumerate(pages)))
        finally:
            save_json(DATA_PATH, records)
            for browser in browsers:
                await browser.close()
            print(
                f"Сохранено в {DATA_PATH.name}. "
                f"Дописано за этот запуск: {stats['done']}, "
                f"с типом и координатами: {stats['filled']}."
            )


if __name__ == "__main__":
    count = WORKERS
    if len(sys.argv) > 1 and sys.argv[1].isdigit():
        count = int(sys.argv[1])
    asyncio.run(run(count))
