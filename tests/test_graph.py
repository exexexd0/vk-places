import pytest

from rag.graph import build_route_graph, initial_state, route_view, validate_route
from rag.models import RoutePlan
from rag.planner import PlannerTimeout
from rag.routing import RoutingError
from rag.selection import select_pair
from tests.test_routing import MatrixRouter


class FixedPlanner:
    def __init__(self, points, precedence=None, fixed_first=None, fixed_last=None):
        self.points = points
        self.precedence = precedence or []
        self.fixed_first = fixed_first
        self.fixed_last = fixed_last

    async def plan(self, user_query, city, available_categories):
        return RoutePlan(
            route_title="Прогулка",
            required_points=self.points,
            precedence=self.precedence,
            fixed_first=self.fixed_first,
            fixed_last=self.fixed_last,
        )


class TimeoutPlanner:
    async def plan(self, user_query, city, available_categories):
        raise PlannerTimeout("LLM не ответила за 3.5 с")


class CatalogRetriever:
    def __init__(self, catalog):
        self.catalog = catalog
        self.calls = []
        self.kwargs = []

    async def search(self, *, category, excluded_org_ids, **kwargs):
        self.calls.append(list(excluded_org_ids))
        self.kwargs.append(kwargs)
        blocked = set(excluded_org_ids)
        return [item for item in self.catalog[category] if item["org_id"] not in blocked]


class RatedRetriever:
    def __init__(self, places):
        self.places = places
        self.cities = []
        self.searches = 0

    async def top_by_rating(self, *, city, limit=4):
        self.cities.append((city, limit))
        return self.places[:limit]

    async def search(self, **kwargs):
        self.searches += 1
        return []


class RankingSelector:
    def __init__(self, source="jev"):
        self.source = source
        self.routes = []
        self.calls = 0

    async def choose(self, *, candidates, route_so_far=None, **kwargs):
        self.calls += 1
        self.routes.append(route_so_far)
        if not candidates:
            return None, None, "empty"
        primary, backup = select_pair(candidates)
        return primary, backup, self.source


def test_select_pair_uses_confidence_and_skips_same_place():
    primary, backup = select_pair(
        [
            _point("low", confidence=0.2),
            _point("same", confidence=0.4),
            _point("high", confidence=0.9),
            _point("same", confidence=0.8),
        ]
    )

    assert primary["org_id"] == "high"
    assert backup["org_id"] == "same"


def test_select_pair_without_backup():
    primary, backup = select_pair([_point("only", confidence=0.7)])

    assert primary["org_id"] == "only"
    assert backup is None


@pytest.mark.asyncio
async def test_graph_keeps_primary_and_backup_for_each_step():
    retriever = CatalogRetriever(
        {
            "food": [_point("food-1", "food", 0.2), _point("food-2", "food", 0.9)],
            "mall": [_point("food-2", "mall", 0.95), _point("mall-1", "mall", 0.7), _point("mall-2", "mall", 0.4)],
        }
    )
    selector = RankingSelector()
    app = build_route_graph(FixedPlanner(["food", "mall"]), retriever, selector)

    state = await app.ainvoke(_state())

    assert [step["primary"]["org_id"] for step in state["steps"]] == ["food-2", "mall-1"]
    assert state["steps"][0]["backup"]["org_id"] == "food-1"
    assert state["steps"][1]["backup"]["org_id"] == "mall-2"
    assert "order" not in state["steps"][0]["backup"]
    assert retriever.calls[1] == ["food-2"]
    assert retriever.kwargs[1].get("previous") is None
    assert selector.routes == [[], []]
    validate_route(route_view(state))


@pytest.mark.asyncio
async def test_graph_skips_empty_category_and_keeps_the_rest():
    app = build_route_graph(
        FixedPlanner(["food", "mall"]),
        CatalogRetriever({"food": [], "mall": [_point("mall-1", "mall", 0.8)]}),
        RankingSelector(),
    )

    state = await app.ainvoke(_state())

    assert state["stopped_reason"] is None
    assert [step["primary"]["org_id"] for step in state["steps"]] == ["mall-1"]


@pytest.mark.asyncio
async def test_graph_stops_when_every_category_is_empty():
    app = build_route_graph(
        FixedPlanner(["food", "mall"]),
        CatalogRetriever({"food": [], "mall": []}),
        RankingSelector(),
    )

    state = await app.ainvoke(_state())

    assert state["stopped_reason"] == "no_candidates"
    assert state["steps"] == []


@pytest.mark.asyncio
async def test_graph_records_fallback_source():
    app = build_route_graph(
        FixedPlanner(["food"]),
        CatalogRetriever({"food": [_point("food-1", "food", 0.8), _point("food-2", "food", 0.3)]}),
        RankingSelector(source="fallback"),
    )

    state = await app.ainvoke(_state())

    assert state["steps"][0]["selection_source"] == "fallback"
    assert state["steps"][0]["primary"]["org_id"] == "food-1"


@pytest.mark.asyncio
async def test_graph_uses_rating_fallback_without_jev():
    selector = RankingSelector()
    retriever = RatedRetriever(
        [
            _point("park-1", "nature", rating=4.9),
            _point("cafe-1", "food", rating=4.2),
        ]
    )
    app = build_route_graph(TimeoutPlanner(), retriever, selector)

    state = await app.ainvoke(_state())

    assert selector.calls == 0
    assert retriever.searches == 0
    assert retriever.cities == [("Иваново", 4)]
    assert state["route_title"] == "Маршрут по рейтингу"
    assert [step["primary"]["org_id"] for step in state["steps"]] == ["park-1", "cafe-1"]
    assert {step["selection_source"] for step in state["steps"]} == {"rules"}


@pytest.mark.asyncio
async def test_rating_fallback_still_orders_by_road_time():
    selector = RankingSelector()
    retriever = RatedRetriever(
        [
            _point("park-1", "nature", rating=4.9),
            _point("cafe-1", "food", rating=4.2),
        ]
    )
    router = MatrixRouter(
        {
            "park-1": {"park-1": 0, "cafe-1": 1800},
            "cafe-1": {"cafe-1": 0, "park-1": 300},
        }
    )
    app = build_route_graph(TimeoutPlanner(), retriever, selector, router)

    state = await app.ainvoke(_state())

    assert selector.calls == 0
    assert [step["primary"]["org_id"] for step in state["steps"]] == ["park-1", "cafe-1"]
    assert [point["place_id"] for point in state["waypoints"]] == ["cafe-1", "park-1"]
    assert state["total_distance_meters"] == 300
    validate_route(route_view(state))


@pytest.mark.asyncio
async def test_graph_reorders_points_by_osrm_duration():
    retriever = CatalogRetriever(
        {
            "food": [_point("food-2", "food", 0.9, lon=40.1)],
            "mall": [_point("mall-1", "mall", 0.7, lon=40.2)],
        }
    )
    router = MatrixRouter(
        {
            "food-2": {"food-2": 0, "mall-1": 1800},
            "mall-1": {"mall-1": 0, "food-2": 600},
        },
        {
            "food-2": {"food-2": 0, "mall-1": 100},
            "mall-1": {"mall-1": 0, "food-2": 40},
        },
    )
    app = build_route_graph(FixedPlanner(["food", "mall"]), retriever, RankingSelector(), router)

    state = await app.ainvoke(_state())
    payload = route_view(state)

    assert [step["primary"]["org_id"] for step in state["steps"]] == ["food-2", "mall-1"]
    assert [point["place_id"] for point in payload["waypoints"]] == ["mall-1", "food-2"]
    assert [point["suggested_time"] for point in payload["waypoints"]] == ["10:00", "11:10"]
    assert payload["route_geometry"] == "encoded-polyline"
    assert payload["total_distance_meters"] == 40
    assert payload["total_duration_minutes"] == 160
    assert payload["transport_mode"] == "walk"
    validate_route(payload)


@pytest.mark.asyncio
async def test_graph_keeps_requested_category_order():
    retriever = CatalogRetriever(
        {
            "food": [_point("food-2", "food", 0.9)],
            "mall": [_point("mall-1", "mall", 0.7)],
        }
    )
    router = MatrixRouter(
        {
            "food-2": {"food-2": 0, "mall-1": 1800},
            "mall-1": {"mall-1": 0, "food-2": 600},
        }
    )
    app = build_route_graph(
        FixedPlanner(["food", "mall"], precedence=[["food", "mall"]]),
        retriever,
        RankingSelector(),
        router,
    )

    state = await app.ainvoke(_state())

    assert [point["place_id"] for point in state["waypoints"]] == ["food-2", "mall-1"]
    assert [point["suggested_time"] for point in state["waypoints"]] == ["10:00", "12:00"]
    assert state["total_duration_minutes"] == 180


@pytest.mark.asyncio
async def test_graph_pins_the_finish_category():
    retriever = CatalogRetriever(
        {
            "food": [_point("food-2", "food", 0.9)],
            "station": [_point("station-1", "station", 0.8)],
        }
    )
    router = MatrixRouter(
        {
            "food-2": {"food-2": 0, "station-1": 900},
            "station-1": {"station-1": 0, "food-2": 100},
        }
    )
    app = build_route_graph(
        FixedPlanner(["food", "station"], fixed_last="station"),
        retriever,
        RankingSelector(),
        router,
    )

    state = await app.ainvoke(
        initial_state("закончиться у вокзала", "Иваново", ["food", "station"])
    )

    assert [point["place_id"] for point in state["waypoints"]] == ["food-2", "station-1"]
    assert state["waypoints"][-1]["suggested_time"] == "11:45"


@pytest.mark.asyncio
async def test_graph_reports_osrm_outage():
    retriever = CatalogRetriever(
        {
            "food": [_point("food-1", "food", 0.9)],
            "mall": [_point("mall-1", "mall", 0.7)],
        }
    )

    class DownRouter:
        async def table(self, places, profile):
            raise RoutingError("OSRM недоступен")

        async def route(self, places, profile):
            raise AssertionError("route")

    app = build_route_graph(FixedPlanner(["food", "mall"]), retriever, RankingSelector(), DownRouter())

    with pytest.raises(RoutingError, match="OSRM недоступен"):
        await app.ainvoke(_state())


def _state():
    return initial_state(
        "хочу поесть и зайти в торговый центр",
        "Иваново",
        ["food", "mall", "nature"],
    )


def _point(org_id, category="food", confidence=0.5, rating=4.5, lon=40.97, lat=57.0):
    return {
        "org_id": org_id,
        "name": org_id,
        "address": "ул. Ленина, 1",
        "lat": lat,
        "lon": lon,
        "rating": rating,
        "category": category,
        "confidence": confidence,
        "distance": 1 - confidence,
    }
