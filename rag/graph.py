from typing import TypedDict

from langgraph.graph import END, StateGraph

from rag.models import limit_categories

PUBLIC_FIELDS = (
    "org_id",
    "name",
    "address",
    "lat",
    "lon",
    "rating",
    "category",
    "confidence",
)


class RouteState(TypedDict):
    user_query: str
    city: str
    available_categories: list[str]
    route_title: str
    required_points: list[str]
    current_index: int
    candidates: list[dict]
    primary: dict | None
    backup: dict | None
    steps: list[dict]
    selection_source: str
    stopped_reason: str | None


def initial_state(user_query: str, city: str, available_categories: list[str]) -> RouteState:
    return {
        "user_query": user_query,
        "city": city,
        "available_categories": list(available_categories),
        "route_title": "",
        "required_points": [],
        "current_index": 0,
        "candidates": [],
        "primary": None,
        "backup": None,
        "steps": [],
        "selection_source": "",
        "stopped_reason": None,
    }


def build_route_graph(planner, retriever, selector):
    async def plan_route(state: RouteState) -> dict:
        plan = await planner.plan(
            state["user_query"],
            state["city"],
            state["available_categories"],
        )
        points = limit_categories(plan.required_points, state["available_categories"])
        if not points:
            raise ValueError("Не удалось составить категории маршрута")
        return {
            "route_title": plan.route_title,
            "required_points": points,
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
        previous = state["steps"][-1]["primary"] if state["steps"] else None
        found = await retriever.search(
            user_query=state["user_query"],
            city=state["city"],
            category=category,
            excluded_org_ids=excluded,
            previous=previous,
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
            route_so_far=state["steps"],
        )
        return {"primary": primary, "backup": backup, "selection_source": source}

    async def append_and_check(state: RouteState) -> dict:
        if not state["primary"]:
            return {
                "stopped_reason": "no_candidates",
                "current_index": len(state["required_points"]),
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

    def route_next(state: RouteState) -> str:
        if state["current_index"] < len(state["required_points"]):
            return "continue"
        return "done"

    graph = StateGraph(RouteState)
    graph.add_node("plan_route", plan_route)
    graph.add_node("retrieve_candidates", retrieve_candidates)
    graph.add_node("select_candidate", select_candidate)
    graph.add_node("append_and_check", append_and_check)
    graph.set_entry_point("plan_route")
    graph.add_edge("plan_route", "retrieve_candidates")
    graph.add_edge("retrieve_candidates", "select_candidate")
    graph.add_edge("select_candidate", "append_and_check")
    graph.add_conditional_edges(
        "append_and_check",
        route_next,
        {"continue": "retrieve_candidates", "done": END},
    )
    return graph.compile()


def public_place(place: dict, order: int | None = None) -> dict:
    payload = {field: place.get(field) for field in PUBLIC_FIELDS}
    if order is not None:
        payload["order"] = order
    return payload


def route_view(state: dict) -> dict:
    return {
        "route_title": state["route_title"],
        "city": state["city"],
        "user_query": state["user_query"],
        "required_points": state["required_points"],
        "stopped_reason": state.get("stopped_reason"),
        "steps": state["steps"],
    }


def validate_route(payload: dict) -> None:
    steps = payload.get("steps") or []
    if not steps:
        raise ValueError("Маршрут не содержит шагов")
    if payload.get("stopped_reason"):
        raise ValueError(payload["stopped_reason"])
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
