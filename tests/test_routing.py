from uuid import UUID

import httpx
import pytest

from rag.routing import (
    INF,
    TRANSIT_NOTE,
    OpenPath,
    OsrmRoute,
    OsrmRouter,
    RouteLeg,
    RoutingError,
    assemble_itinerary,
    build_schedule,
    held_karp,
    osrm_profile,
    visit_minutes,
)


class MatrixRouter:
    def __init__(self, durations, distances=None, geometry="encoded-polyline"):
        self.durations = durations
        self.distances = distances or durations
        self.geometry = geometry
        self.table_calls = []
        self.route_calls = []
        self.profiles = []

    async def table(self, places, profile):
        self.profiles.append(profile)
        ids = [place["org_id"] for place in places]
        self.table_calls.append(ids)
        return self._matrix(ids, self.durations), self._matrix(ids, self.distances)

    async def route(self, places, profile):
        self.profiles.append(profile)
        ids = [place["org_id"] for place in places]
        self.route_calls.append(ids)
        legs = []
        total_duration = 0.0
        total_distance = 0.0
        for left, right in zip(ids, ids[1:]):
            duration = self.durations[left][right]
            distance = self.distances[left][right]
            legs.append(RouteLeg(duration, distance))
            total_duration += duration
            total_distance += distance
        return OsrmRoute(total_distance, total_duration, self.geometry, legs)

    def _matrix(self, ids, table):
        return [[table[left][right] for right in ids] for left in ids]


def test_osrm_profile_maps_transit_to_walking():
    assert osrm_profile("walk") == ("foot", None)
    assert osrm_profile("car") == ("car", None)
    assert osrm_profile("transit") == ("foot", TRANSIT_NOTE)


def test_visit_minutes_use_category_defaults():
    assert visit_minutes({"category": "food"}) == 90
    assert visit_minutes({"category": "nature"}) == 45
    assert visit_minutes({"category": "station"}) == 15
    assert visit_minutes({"category": "food", "avg_visit_duration_min": 25}) == 25


def test_held_karp_prefers_shorter_distance_when_duration_matches():
    durations = [
        [0, 10, 100],
        [10, 0, 10],
        [100, 10, 0],
    ]
    distances = [
        [0, 30, 100],
        [5, 0, 30],
        [100, 5, 0],
    ]

    path = held_karp(durations, distances, [], None, None)

    assert path == OpenPath([2, 1, 0], 20, 10)


def test_held_karp_respects_precedence_and_fixed_ends():
    durations = [
        [0, 10, 100],
        [10, 0, 10],
        [100, 10, 0],
    ]
    distances = [
        [0, 30, 100],
        [5, 0, 30],
        [100, 5, 0],
    ]

    forced = held_karp(durations, distances, [(0, 2)], None, None)
    assert forced.order == [0, 1, 2]

    ending = held_karp(durations, distances, [], None, 2)
    assert ending.order[-1] == 2

    starting = held_karp(durations, distances, [], 2, None)
    assert starting.order[0] == 2
    assert starting.order == [2, 1, 0]


def test_held_karp_rejects_more_than_ten_points():
    matrix = [[0] * 11 for _ in range(11)]

    with pytest.raises(RoutingError, match="10"):
        held_karp(matrix, matrix, [], None, None)


def test_schedule_starts_at_ten_and_adds_visit_plus_leg():
    places = [
        {"org_id": "cafe", "name": "Кафе", "lat": 57.0, "lon": 40.9, "category": "food"},
        {"org_id": "park", "name": "Парк", "lat": 57.1, "lon": 41.0, "category": "nature"},
    ]

    waypoints = build_schedule(places, [1800])

    assert [point["suggested_time"] for point in waypoints] == ["10:00", "12:00"]
    assert [point["order"] for point in waypoints] == [1, 2]
    assert waypoints[0]["place_id"] == "cafe"


@pytest.mark.asyncio
async def test_single_point_skips_osrm():
    router = MatrixRouter({})
    step = _step("cafe", "food")

    built = await assemble_itinerary(
        [step],
        precedence=[],
        fixed_first=None,
        fixed_last=None,
        transport_mode="transit",
        router=router,
    )

    assert router.table_calls == []
    assert router.route_calls == []
    assert built["route_geometry"] == ""
    assert built["total_distance_meters"] == 0
    assert built["total_duration_minutes"] == 90
    assert built["waypoints"][0]["suggested_time"] == "10:00"
    assert built["transport_note"] == TRANSIT_NOTE
    UUID(built["route_id"])


@pytest.mark.asyncio
async def test_backup_replaces_only_unreachable_primary():
    durations = {
        "a": {"a": 0, "b": INF, "c": 10, "a2": 1, "b2": INF},
        "b": {"a": INF, "b": 0, "c": INF, "a2": INF, "b2": INF},
        "c": {"a": 10, "b": INF, "c": 0, "a2": 1, "b2": 8},
        "a2": {"a": 1, "b": INF, "c": 1, "a2": 0, "b2": INF},
        "b2": {"a": INF, "b": INF, "c": 8, "a2": INF, "b2": 0},
    }
    distances = {
        "a": {"a": 0, "b": INF, "c": 4, "a2": 1, "b2": INF},
        "b": {"a": INF, "b": 0, "c": INF, "a2": INF, "b2": INF},
        "c": {"a": 4, "b": INF, "c": 0, "a2": 1, "b2": 3},
        "a2": {"a": 1, "b": INF, "c": 1, "a2": 0, "b2": INF},
        "b2": {"a": INF, "b": INF, "c": 3, "a2": INF, "b2": 0},
    }
    router = MatrixRouter(durations, distances)
    steps = [
        _step("a", "food", backup="unused"),
        _step("b", "nature", backup="b2"),
        _step("c", "sight"),
    ]

    built = await assemble_itinerary(
        steps,
        precedence=[],
        fixed_first=None,
        fixed_last=None,
        transport_mode="walk",
        router=router,
    )

    assert router.table_calls[0] == ["a", "b", "c"]
    assert "b2" in router.table_calls[1]
    assert "a2" not in router.table_calls[1]
    assert {point["place_id"] for point in built["waypoints"]} == {"a", "b2", "c"}
    assert built["route_geometry"] == "encoded-polyline"
    assert router.profiles == ["foot", "foot", "foot"]


@pytest.mark.asyncio
async def test_unreachable_set_is_an_error_without_straight_line():
    durations = {
        "a": {"a": 0, "b": INF, "a2": INF},
        "b": {"a": INF, "b": 0, "a2": INF},
        "a2": {"a": INF, "b": INF, "a2": 0},
    }
    router = MatrixRouter(durations)

    with pytest.raises(RoutingError, match="конечного пути"):
        await assemble_itinerary(
            [_step("a", "food", backup="a2"), _step("b", "nature")],
            precedence=[],
            fixed_first=None,
            fixed_last=None,
            transport_mode="car",
            router=router,
        )

    assert router.route_calls == []
    assert router.profiles == ["car", "car"]


@pytest.mark.asyncio
async def test_osrm_table_treats_null_as_unreachable_and_requests_polyline():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/table/"):
            seen["table_profile"] = request.url.path.split("/")[3]
            seen["table_coords"] = request.url.path.split("/")[4]
            seen["annotations"] = request.url.params["annotations"]
            return httpx.Response(
                200,
                json={
                    "code": "Ok",
                    "durations": [[0, None], [12, 0]],
                    "distances": [[0, None], [80, 0]],
                },
            )
        seen["overview"] = request.url.params["overview"]
        seen["geometries"] = request.url.params["geometries"]
        seen["steps"] = request.url.params["steps"]
        return httpx.Response(
            200,
            json={
                "code": "Ok",
                "routes": [
                    {
                        "distance": 80,
                        "duration": 12,
                        "geometry": "abc",
                        "legs": [{"distance": 80, "duration": 12}],
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        router = OsrmRouter("http://osrm.test", client=client)
        built = await assemble_itinerary(
            [_step("cafe", "food", lon=40.97, lat=57.0), _step("park", "nature", lon=40.98, lat=57.01)],
            precedence=[],
            fixed_first=None,
            fixed_last=None,
            transport_mode="walk",
            router=router,
        )

    assert seen["table_profile"] == "foot"
    assert seen["table_coords"] == "40.97,57.0;40.98,57.01"
    assert seen["annotations"] == "duration,distance"
    assert seen["overview"] == "full"
    assert seen["geometries"] == "polyline"
    assert seen["steps"] == "false"
    assert built["route_geometry"] == "abc"
    assert built["total_distance_meters"] == 80
    assert [point["place_id"] for point in built["waypoints"]] == ["park", "cafe"]


@pytest.mark.asyncio
async def test_osrm_outage_is_a_controlled_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        router = OsrmRouter("http://osrm.test", client=client)
        with pytest.raises(RoutingError, match="OSRM недоступен"):
            await router.table([_place("cafe", "food")], "foot")


def _step(org_id, category, backup=None, lon=40.97, lat=57.0):
    step = {
        "category": category,
        "primary": _place(org_id, category, lon, lat),
        "backup": _place(backup, category, lon + 0.01, lat + 0.01) if backup else None,
    }
    return step


def _place(org_id, category, lon=40.97, lat=57.0):
    return {
        "org_id": org_id,
        "name": org_id,
        "lat": lat,
        "lon": lon,
        "category": category,
    }
