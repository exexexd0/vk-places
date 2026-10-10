import asyncio

import pytest

from rag.models import CategoryOrder, RoutePlan, apply_plan_limits
from rag.planner import LLM_TIMEOUT_SECONDS, LlmPlanner, PlannerTimeout
from rag.prompts import retrieval_query


def test_llm_timeout_is_three_and_a_half_seconds():
    assert LLM_TIMEOUT_SECONDS == 3.5


def test_apply_plan_limits_keeps_only_explicit_order():
    plan = RoutePlan(
        route_title="День",
        required_points=["food", "nature", "mall", "sport", "sight"],
        precedence=[["food", "nature"], ["station", "food"], ["food", "food"]],
        fixed_first="station",
        fixed_last="nature",
    )

    limited = apply_plan_limits(plan, ["food", "nature", "mall", "sport", "culture"])

    assert limited.required_points == ["food", "nature", "mall", "sport"]
    assert limited.precedence == [CategoryOrder(before="food", after="nature")]
    assert limited.fixed_first is None
    assert limited.fixed_last == "nature"


def test_apply_plan_limits_rejects_unknown_categories():
    plan = RoutePlan(route_title="День", required_points=["food"])

    with pytest.raises(ValueError, match="доступные категории"):
        apply_plan_limits(plan, ["mall"])


def test_retrieval_query_does_not_mention_previous_place():
    text = retrieval_query("хочу в парк и поужинать", "nature")

    assert "Предыдущая" not in text
    assert "парки" in text


@pytest.mark.asyncio
async def test_planner_timeout(monkeypatch):
    planner = LlmPlanner.__new__(LlmPlanner)

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.2)

    planner._draft = slow
    monkeypatch.setattr("rag.planner.LLM_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(PlannerTimeout, match="3.5"):
        await planner.plan("хочу в парк", "Иваново", ["nature"])
