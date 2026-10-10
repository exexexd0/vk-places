import pytest

from rag.graph import build_route_graph, initial_state, validate_route
from rag.models import RoutePlan
from rag.selection import select_pair


class FixedPlanner:
    def __init__(self, points):
        self.points = points

    async def plan(self, user_query, city, available_categories):
        return RoutePlan(route_title="Прогулка", required_points=self.points)


class CatalogRetriever:
    def __init__(self, catalog):
        self.catalog = catalog
        self.calls = []

    async def search(self, *, category, excluded_org_ids, **kwargs):
        self.calls.append(list(excluded_org_ids))
        blocked = set(excluded_org_ids)
        return [item for item in self.catalog[category] if item["org_id"] not in blocked]


class RankingSelector:
    def __init__(self, source="jev"):
        self.source = source

    async def choose(self, *, candidates, **kwargs):
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
    app = build_route_graph(
        FixedPlanner(["food", "mall"]),
        retriever,
        RankingSelector(),
    )

    state = await app.ainvoke(_state())

    assert [step["primary"]["org_id"] for step in state["steps"]] == ["food-2", "mall-1"]
    assert state["steps"][0]["backup"]["org_id"] == "food-1"
    assert state["steps"][1]["backup"]["org_id"] == "mall-2"
    assert "order" not in state["steps"][0]["backup"]
    assert retriever.calls[1] == ["food-2"]
    validate_route(
        {
            "steps": state["steps"],
            "stopped_reason": state["stopped_reason"],
        }
    )


@pytest.mark.asyncio
async def test_graph_stops_when_category_has_no_candidates():
    app = build_route_graph(
        FixedPlanner(["food", "mall"]),
        CatalogRetriever({"food": [_point("food-1", "food", 0.8)], "mall": []}),
        RankingSelector(),
    )

    state = await app.ainvoke(_state())

    assert state["stopped_reason"] == "no_candidates"
    assert [step["primary"]["org_id"] for step in state["steps"]] == ["food-1"]
    assert state["steps"][0]["backup"] is None


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


def _state():
    return initial_state(
        "хочу поесть и зайти в торговый центр",
        "Иваново",
        ["food", "mall"],
    )


def _point(org_id, category="food", confidence=0.5):
    return {
        "org_id": org_id,
        "name": org_id,
        "address": "ул. Ленина, 1",
        "lat": 57.0,
        "lon": 40.97,
        "rating": 4.5,
        "category": category,
        "confidence": confidence,
        "distance": 1 - confidence,
    }
