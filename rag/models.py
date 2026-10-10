from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from rag.categories import CANONICAL_CATEGORIES

CategoryName = Literal[
    "food",
    "culture",
    "nature",
    "sight",
    "entertainment",
    "mall",
    "station",
    "sport",
]


class CategoryOrder(BaseModel):
    before: CategoryName
    after: CategoryName

    @model_validator(mode="before")
    @classmethod
    def from_pair(cls, value):
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return {"before": value[0], "after": value[1]}
        return value


class RoutePlan(BaseModel):
    route_title: str = Field(min_length=1)
    required_points: list[CategoryName]
    precedence: list[CategoryOrder] = Field(default_factory=list)
    fixed_first: CategoryName | None = None
    fixed_last: CategoryName | None = None

    @field_validator("fixed_first", "fixed_last", mode="before")
    @classmethod
    def empty_endpoint(cls, value):
        if value in ("", None):
            return None
        return value


def limit_categories(categories: list[str], available: list[str], limit: int = 4) -> list[str]:
    allowed = set(available) & set(CANONICAL_CATEGORIES)
    selected: list[str] = []
    for category in categories:
        if category in allowed and category not in selected:
            selected.append(category)
        if len(selected) == limit:
            break
    return selected


def constraints_for(plan: RoutePlan, points: list[str]) -> tuple[list[list[str]], str | None, str | None]:
    allowed = set(points)
    precedence: list[list[str]] = []
    seen: set[tuple[str, str]] = set()
    for item in plan.precedence:
        pair = (item.before, item.after)
        if pair[0] not in allowed or pair[1] not in allowed or pair[0] == pair[1] or pair in seen:
            continue
        seen.add(pair)
        precedence.append([pair[0], pair[1]])
    fixed_first = plan.fixed_first if plan.fixed_first in allowed else None
    fixed_last = plan.fixed_last if plan.fixed_last in allowed else None
    return precedence, fixed_first, fixed_last


def apply_plan_limits(plan: RoutePlan, available: list[str], limit: int = 4) -> RoutePlan:
    points = limit_categories(plan.required_points, available, limit)
    if not points:
        raise ValueError("Планировщик не вернул доступные категории")
    precedence, fixed_first, fixed_last = constraints_for(plan, points)
    return plan.model_copy(
        update={
            "required_points": points,
            "precedence": [CategoryOrder(before=before, after=after) for before, after in precedence],
            "fixed_first": fixed_first,
            "fixed_last": fixed_last,
        }
    )
