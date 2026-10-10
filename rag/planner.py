import json
import logging
import re

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from rag.models import RoutePlan, limit_categories
from rag.prompts import PLAN_ROUTE_JSON_SUFFIX, PLAN_ROUTE_SYSTEM, plan_route_user
from rag.settings import Settings

logger = logging.getLogger("rag.planner")
JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


class LlmPlanner:
    def __init__(self, settings: Settings):
        self.model = ChatOpenAI(
            model=settings.chat_model,
            api_key=settings.routerai_api_key,
            base_url=settings.routerai_base_url,
            temperature=0,
            timeout=40,
            max_retries=2,
        )

    async def plan(self, user_query: str, city: str, available_categories: list[str]) -> RoutePlan:
        messages = [
            SystemMessage(content=PLAN_ROUTE_SYSTEM),
            HumanMessage(content=plan_route_user(user_query, city, available_categories)),
        ]
        try:
            raw_plan = await self.model.with_structured_output(RoutePlan).ainvoke(messages)
            plan = raw_plan if isinstance(raw_plan, RoutePlan) else RoutePlan.model_validate(raw_plan)
        except Exception as error:
            logger.warning("Структурированный ответ недоступен: %s", error)
            plan = await self._plan_from_json(messages)
        points = limit_categories(plan.required_points, available_categories)
        if not points:
            raise ValueError("Планировщик не вернул доступные категории")
        return plan.model_copy(update={"required_points": points})

    async def _plan_from_json(self, messages: list) -> RoutePlan:
        response = await self.model.ainvoke(
            [*messages, HumanMessage(content=PLAN_ROUTE_JSON_SUFFIX)]
        )
        match = JSON_OBJECT.search(_message_text(response))
        if match is None:
            raise ValueError("Планировщик не вернул JSON")
        return RoutePlan.model_validate(json.loads(match.group(0)))


def _message_text(message) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    return str(content)
