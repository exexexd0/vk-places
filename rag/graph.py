import logging
from typing import TypedDict

from langgraph.graph import END, StateGraph

from rag.models import constraints_for, limit_categories
from rag.planner import PlannerTimeout
from rag.routing import assemble_itinerary

logger = logging.getLogger("rag.graph")

PUBLIC_FIELDS = (
    "org_id",
    "name",
    "address",
    "lat",
    "lon",
    "rating",
    "category",
    "confidence",
    "avg_visit_duration_min",
)


class RouteState(TypedDict):
    user_query: str
    city: str
    available_categories: list[str]
    transport_mode: str
    route_title: str
    required_points: list[str]
    precedence: list[list[str]]
    fixed_first: str | None
    fixed_last: str | None
    current_index: int
    candidates: list[dict]
    primary: dict | None
    backup: dict | None
    steps: list[dict]
    selection_source: str
    stopped_reason: str | None
    route_id: str | None
    waypoints: list[dict]
    route_geometry: str
    total_distance_meters: int | None
    total_duration_minutes: int | None
    transport_note: str | None


def initial_state(
    user_query: str,
    city: str,
    available_categories: list[str],
    transport_mode: str = "walk",
) -> RouteState:
    return {
        "user_query": user_query,
        "city": city,
        "available_categories": list(available_categories),
        "transport_mode": transport_mode,
        "route_title": "",
        "required_points": [],
        "precedence": [],
        "fixed_first": None,
        "fixed_last": None,
        "current_index": 0,
        "candidates": [],
        "primary": None,
        "backup": None,
        "steps": [],
        "selection_source": "",
        "stopped_reason": None,
        "route_id": None,
        "waypoints": [],
        "route_geometry": "",
        "total_distance_meters": None,
        "total_duration_minutes": None,
        "transport_note": None,
    }


def build_route_graph(planner, retriever, selector, router=None):
    async def plan_route(state: RouteState) -> dict:
        try:
            plan = await planner.plan(
                state["user_query"],
                state["city"],
                state["available_categories"],
            )
        except PlannerTimeout:
            logger.warning("LLM не ответила за 3.5 с, выбор по рейтингу")
            places = await retriever.top_by_rating(city=state["city"], limit=4)
            steps = _rule_steps(places)
            return {
                "route_title": "Маршрут по рейтингу",
                "required_points": [step["category"] for step in steps],
                "precedence": [],
                "fixed_first": None,
                "fixed_last": None,
                "steps": steps,
                "current_index": len(steps),
                "candidates": [],
                "primary": None,
                "backup": None,
                "selection_source": "rules",
                "stopped_reason": None,
            }
        points = limit_categories(plan.required_points, state["available_categories"])
        if not points:
            raise ValueError("Не удалось составить категории маршрута")
        precedence, fixed_first, fixed_last = constraints_for(plan, points)
        return {
            "route_title": plan.route_title,
            "required_points": points,
            "precedence": precedence,
            "fixed_first": fixed_first,
            "fixed_last": fixed_last,
            "current_index": 0,
            "steps": [],
            "candidates": [],
            "primary": None,
            "backup": None,
            "selection_source": "",
            "stopped_reason": None,
        }

    async def retrieve_candidates(state: RouteState) -> dict:
        category = state["required_points"][state["current_index"]]
        excluded = [
            step["primary"]["org_id"]
            for step in state["steps"]
            if step.get("primary")
        ]
        found = await retriever.search(
            user_query=state["user_query"],
            city=state["city"],
            category=category,
            excluded_org_ids=excluded,
        )
        blocked = set(excluded)
        candidates = [item for item in found if item.get("org_id") not in blocked]
        return {"candidates": candidates, "primary": None, "backup": None}

    async def select_candidate(state: RouteState) -> dict:
        primary, backup, source = await selector.choose(
            user_query=state["user_query"],
            city=state["city"],
            category=state["required_points"][state["current_index"]],
            candidates=state["candidates"],
            route_so_far=[],
        )
        return {"primary": primary, "backup": backup, "selection_source": source}

    async def append_and_check(state: RouteState) -> dict:
        if not state["primary"]:
            return {
                "current_index": state["current_index"] + 1,
                "candidates": [],
                "primary": None,
                "backup": None,
            }
        order = len(state["steps"]) + 1
        step = {
            "order": order,
            "category": state["required_points"][state["current_index"]],
            "selection_source": state["selection_source"],
            "primary": public_place(state["primary"], order=order),
            "backup": public_place(state["backup"]) if state["backup"] else None,
        }
        return {
            "steps": [*state["steps"], step],
            "current_index": state["current_index"] + 1,
            "candidates": [],
            "primary": None,
            "backup": None,
        }

    async def optimize_route(state: RouteState) -> dict:
        if not state["steps"]:
            return {"stopped_reason": "no_candidates"}
        if router is None:
            return {}
        built = await assemble_itinerary(
            state["steps"],
            precedence=state["precedence"],
            fixed_first=state["fixed_first"],
            fixed_last=state["fixed_last"],
            transport_mode=state["transport_mode"],
            router=router,
        )
        return built

    def next_stage(state: RouteState) -> str:
        if state["current_index"] < len(state["required_points"]):
            return "retrieve"
        return "optimize"

    graph = StateGraph(RouteState)
    graph.add_node("plan_route", plan_route)
    graph.add_node("retrieve_candidates", retrieve_candidates)
    graph.add_node("select_candidate", select_candidate)
    graph.add_node("append_and_check", append_and_check)
    graph.add_node("optimize_route", optimize_route)
    graph.set_entry_point("plan_route")
    graph.add_conditional_edges(
        "plan_route",
        next_stage,
        {"retrieve": "retrieve_candidates", "optimize": "optimize_route"},
    )
    graph.add_edge("retrieve_candidates", "select_candidate")
    graph.add_edge("select_candidate", "append_and_check")
    graph.add_conditional_edges(
        "append_and_check",
        next_stage,
        {"retrieve": "retrieve_candidates", "optimize": "optimize_route"},
    )
    graph.add_edge("optimize_route", END)
    return graph.compile()


def public_place(place: dict, order: int | None = None) -> dict:
    payload = {field: place.get(field) for field in PUBLIC_FIELDS}
    if order is not None:
        payload["order"] = order
    return payload


def _rule_steps(places: list[dict]) -> list[dict]:
    steps = []
    for place in places:
        order = len(steps) + 1
        steps.append(
            {
                "order": order,
                "category": place["category"],
                "selection_source": "rules",
                "primary": public_place(place, order=order),
                "backup": None,
            }
        )
    return steps


def route_view(state: dict) -> dict:
    payload = {
        "route_title": state["route_title"],
        "city": state["city"],
        "user_query": state["user_query"],
        "transport_mode": state.get("transport_mode") or "walk",
        "required_points": state["required_points"],
        "precedence": state.get("precedence") or [],
        "fixed_first": state.get("fixed_first"),
        "fixed_last": state.get("fixed_last"),
        "stopped_reason": state.get("stopped_reason"),
        "steps": state["steps"],
        "waypoints": state.get("waypoints") or [],
        "route_geometry": state.get("route_geometry") or "",
        "total_distance_meters": state.get("total_distance_meters"),
        "total_duration_minutes": state.get("total_duration_minutes"),
    }
    if state.get("route_id"):
        payload["route_id"] = state["route_id"]
    if state.get("transport_note"):
        payload["transport_note"] = state["transport_note"]
    return payload


def validate_route(payload: dict) -> None:
    if payload.get("stopped_reason"):
        raise ValueError(payload["stopped_reason"])
    steps = payload.get("steps") or []
    if not steps:
        raise ValueError("Маршрут не содержит шагов")
    seen: set[str] = set()
    for index, step in enumerate(steps, start=1):
        primary = step.get("primary")
        backup = step.get("backup")
        if not primary:
            raise ValueError(f"У шага {index} нет основной точки")
        if primary.get("order") != index:
            raise ValueError("Порядок основных точек нарушен")
        for field in ("org_id", "name", "lat", "lon", "category"):
            if primary.get(field) in (None, ""):
                raise ValueError(f"У основной точки нет поля {field}")
        if primary["org_id"] in seen:
            raise ValueError("Основная точка повторяется")
        seen.add(primary["org_id"])
        if backup and backup.get("org_id") == primary["org_id"]:
            raise ValueError("Запасная точка совпадает с основной")
        if backup and "order" in backup:
            raise ValueError("Порядок назначен запасной точке")
    waypoints = payload.get("waypoints") or []
    for index, point in enumerate(waypoints, start=1):
        if point.get("order") != index:
            raise ValueError("Порядок точек маршрута нарушен")
        for field in ("place_id", "name", "lat", "lon", "category", "suggested_time"):
            if point.get(field) in (None, ""):
                raise ValueError(f"У точки маршрута нет поля {field}")
