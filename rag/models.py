from typing import Literal

from pydantic import BaseModel, Field

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


class RoutePlan(BaseModel):
    route_title: str = Field(min_length=1)
    required_points: list[CategoryName]


def limit_categories(categories: list[str], available: list[str], limit: int = 4) -> list[str]:
    allowed = set(available) & set(CANONICAL_CATEGORIES)
    selected: list[str] = []
    for category in categories:
        if category in allowed and category not in selected:
            selected.append(category)
        if len(selected) == limit:
            break
    return selected
