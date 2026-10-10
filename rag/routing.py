import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from uuid import uuid4

import httpx

INF = math.inf
MAX_POINTS = 10
OSRM_TIMEOUT_SECONDS = 10
DAY_START = time(10, 0)
TRANSIT_NOTE = "Отдельного графа общественного транспорта нет, маршрут построен пешком."
VISIT_MINUTES = {
    "food": 90,
    "culture": 60,
    "sight": 60,
    "entertainment": 60,
    "nature": 45,
    "mall": 60,
    "sport": 60,
    "station": 15,
}


class RoutingError(RuntimeError):
    pass


@dataclass(frozen=True)
class OpenPath:
    order: list[int]
    duration: float
    distance: float


@dataclass(frozen=True)
class RouteLeg:
    duration: float
    distance: float


@dataclass(frozen=True)
class OsrmRoute:
    distance: float
    duration: float
    geometry: str
    legs: list[RouteLeg]


def osrm_profile(transport_mode: str) -> tuple[str, str | None]:
    if transport_mode == "car":
        return "car", None
    if transport_mode == "transit":
        return "foot", TRANSIT_NOTE
    if transport_mode == "walk":
        return "foot", None
    raise ValueError(f"Неизвестный transport_mode: {transport_mode}")


def visit_minutes(place: dict) -> int:
    raw = place.get("avg_visit_duration_min")
    if raw is not None:
        minutes = int(raw)
        if minutes >= 0:
            return minutes
    category = place.get("category")
    return VISIT_MINUTES.get(category, 60)


def held_karp(
    durations: list[list[float]],
    distances: list[list[float]],
    precedence: list[tuple[int, int]],
    fixed_first: int | None,
    fixed_last: int | None,
) -> OpenPath | None:
    count = len(durations)
    if count == 0:
        return None
    if count > MAX_POINTS:
        raise RoutingError("Маршрут длиннее 10 точек")
    _square(durations, count)
    _square(distances, count)
    if count == 1:
        return OpenPath([0], 0.0, 0.0)

    required = [set() for _ in range(count)]
    for before, after in precedence:
        if before == after or not 0 <= before < count or not 0 <= after < count:
            continue
        required[after].add(before)

    unset = (INF, INF)
    cost = [[unset for _ in range(count)] for _ in range(1 << count)]
    parent = [[-1 for _ in range(count)] for _ in range(1 << count)]

    if fixed_first is not None:
        if not required[fixed_first]:
            cost[1 << fixed_first][fixed_first] = (0.0, 0.0)
    else:
        for index in range(count):
            if required[index]:
                continue
            cost[1 << index][index] = (0.0, 0.0)

    for mask in range(1 << count):
        for end in range(count):
            current = cost[mask][end]
            if math.isinf(current[0]) or (mask & (1 << end)) == 0:
                continue
            for nxt in range(count):
                if mask & (1 << nxt):
                    continue
                if not required[nxt].issubset(_bits(mask)):
                    continue
                leg_duration = durations[end][nxt]
                if math.isinf(leg_duration):
                    continue
                leg_distance = distances[end][nxt]
                if math.isinf(leg_distance):
                    continue
                candidate = (current[0] + leg_duration, current[1] + leg_distance)
                next_mask = mask | (1 << nxt)
                if candidate < cost[next_mask][nxt]:
                    cost[next_mask][nxt] = candidate
                    parent[next_mask][nxt] = end

    full = (1 << count) - 1
    ends = [fixed_last] if fixed_last is not None else range(count)
    best_end = None
    best_cost = unset
    for end in ends:
        if cost[full][end] < best_cost:
            best_cost = cost[full][end]
            best_end = end
    if best_end is None or math.isinf(best_cost[0]):
        return None

    order = []
    mask = full
    node = best_end
    while node != -1:
        order.append(node)
        previous = parent[mask][node]
        mask ^= 1 << node
        node = previous
    order.reverse()
    return OpenPath(order, best_cost[0], best_cost[1])


def build_schedule(places: list[dict], leg_seconds: list[float]) -> list[dict]:
    if len(leg_seconds) != max(len(places) - 1, 0):
        raise RoutingError("OSRM вернул не все плечи маршрута")
    clock = datetime.combine(date.min, DAY_START)
    waypoints = []
    for index, place in enumerate(places):
        waypoints.append(
            {
                "order": index + 1,
                "place_id": place["org_id"],
                "name": place["name"],
                "lat": place["lat"],
                "lon": place["lon"],
                "category": place["category"],
                "suggested_time": clock.strftime("%H:%M"),
            }
        )
        if index < len(places) - 1:
            clock += timedelta(minutes=visit_minutes(place), seconds=leg_seconds[index])
    return waypoints


async def assemble_itinerary(
    steps: list[dict],
    *,
    precedence: list[list[str]],
    fixed_first: str | None,
    fixed_last: str | None,
    transport_mode: str,
    router,
) -> dict:
    if len(steps) > MAX_POINTS:
        raise RoutingError("Маршрут длиннее 10 точек")
    profile, note = osrm_profile(transport_mode)
    if len(steps) == 1:
        place = steps[0]["primary"]
        waypoints = build_schedule([place], [])
        return _payload(
            waypoints,
            distance=0,
            duration_seconds=0,
            geometry="",
            note=note,
            places=[place],
        )
    ordered = await _order_with_backups(steps, precedence, fixed_first, fixed_last, router, profile)
    route = await router.route(ordered, profile)
    if len(route.legs) != len(ordered) - 1:
        raise RoutingError("OSRM вернул не все плечи маршрута")
    waypoints = build_schedule(ordered, [leg.duration for leg in route.legs])
    return _payload(
        waypoints,
        distance=route.distance,
        duration_seconds=route.duration,
        geometry=route.geometry,
        note=note,
        places=ordered,
    )


class OsrmRouter:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.client = client

    async def table(self, places: list[dict], profile: str) -> tuple[list[list[float]], list[list[float]]]:
        payload = await self._get(
            f"/table/v1/{profile}/{_coords(places)}",
            {"annotations": "duration,distance"},
        )
        return _matrix(payload, "durations"), _matrix(payload, "distances")

    async def route(self, places: list[dict], profile: str) -> OsrmRoute:
        payload = await self._get(
            f"/route/v1/{profile}/{_coords(places)}",
            {"overview": "full", "geometries": "polyline", "steps": "false"},
        )
        routes = payload.get("routes") or []
        if not routes:
            raise RoutingError("OSRM не вернул маршрут")
        chosen = routes[0]
        legs = []
        for leg in chosen.get("legs") or []:
            legs.append(RouteLeg(duration=_metric(leg.get("duration")), distance=_metric(leg.get("distance"))))
        return OsrmRoute(
            distance=_metric(chosen.get("distance")),
            duration=_metric(chosen.get("duration")),
            geometry=str(chosen.get("geometry") or ""),
            legs=legs,
        )

    async def _get(self, path: str, params: dict) -> dict:
        url = self.base_url + path
        try:
            if self.client is not None:
                response = await self.client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
            else:
                async with httpx.AsyncClient(timeout=OSRM_TIMEOUT_SECONDS) as client:
                    response = await client.get(url, params=params)
                    response.raise_for_status()
                    payload = response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise RoutingError("OSRM недоступен") from error
        if payload.get("code") not in (None, "Ok"):
            raise RoutingError("OSRM не построил маршрут")
        return payload


def _payload(
    waypoints: list[dict],
    *,
    distance: float,
    duration_seconds: float,
    geometry: str,
    note: str | None,
    places: list[dict] | None = None,
) -> dict:
    visited = places if places is not None else waypoints
    visit_total = sum(visit_minutes(place) for place in visited)
    return {
        "route_id": str(uuid4()),
        "waypoints": waypoints,
        "route_geometry": geometry,
        "total_distance_meters": int(round(distance)),
        "total_duration_minutes": int(round(duration_seconds / 60)) + visit_total,
        "transport_note": note,
    }


async def _order_with_backups(steps, precedence, fixed_first, fixed_last, router, profile):
    chosen = [dict(step["primary"]) for step in steps]
    used_backup = [False] * len(steps)
    pairs, first, last = _index_constraints(steps, precedence, fixed_first, fixed_last)
    while True:
        durations, distances = await router.table(chosen, profile)
        _square(durations, len(chosen))
        _square(distances, len(chosen))
        path = held_karp(durations, distances, pairs, first, last)
        if path is not None:
            return [chosen[index] for index in path.order]
        swap = _next_backup(durations, steps, chosen, used_backup)
        if swap is None:
            raise RoutingError("Нет конечного пути по дорожному графу")
        chosen[swap] = dict(steps[swap]["backup"])
        used_backup[swap] = True


def _index_constraints(steps, precedence, fixed_first, fixed_last):
    position = {step["category"]: index for index, step in enumerate(steps)}
    pairs = []
    for item in precedence:
        before, after = item[0], item[1]
        if before in position and after in position and before != after:
            pairs.append((position[before], position[after]))
    first = position[fixed_first] if fixed_first in position else None
    last = position[fixed_last] if fixed_last in position else None
    return pairs, first, last


def _next_backup(durations, steps, chosen, used_backup) -> int | None:
    available = []
    for index, step in enumerate(steps):
        if used_backup[index]:
            continue
        backup = step.get("backup")
        if not backup or backup.get("org_id") == chosen[index].get("org_id"):
            continue
        available.append(index)
    for index in available:
        if _disconnected(durations, index):
            return index
    if available:
        return available[0]
    return None


def _disconnected(durations, index: int) -> bool:
    for other, row_value in enumerate(durations[index]):
        if other == index:
            continue
        if row_value < INF or durations[other][index] < INF:
            return False
    return True


def _square(matrix: list[list[float]], count: int) -> None:
    if len(matrix) != count or any(len(row) != count for row in matrix):
        raise RoutingError("OSRM вернул матрицу неверного размера")


def _bits(mask: int) -> set[int]:
    bits = set()
    index = 0
    while mask:
        if mask & 1:
            bits.add(index)
        mask >>= 1
        index += 1
    return bits


def _coords(places: list[dict]) -> str:
    return ";".join(f"{float(place['lon'])},{float(place['lat'])}" for place in places)


def _matrix(payload: dict, key: str) -> list[list[float]]:
    rows = payload.get(key)
    if not rows:
        raise RoutingError("OSRM не вернул матрицу")
    return [[_cell(value) for value in row] for row in rows]


def _cell(value) -> float:
    if value is None:
        return INF
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return INF
    return number


def _metric(value) -> float:
    if value is None:
        raise RoutingError("OSRM не вернул маршрут")
    return float(value)
