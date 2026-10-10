import logging

import httpx

from rag.prompts import JEV_INSTRUCTIONS, candidate_criterion
from rag.selection import align_probabilities, fallback_ranked, select_pair
from rag.settings import Settings

logger = logging.getLogger("rag.jev")


class JevSelector:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client

    async def choose(
        self,
        *,
        user_query: str,
        city: str,
        category: str,
        candidates: list[dict],
        route_so_far: list[dict],
    ) -> tuple[dict | None, dict | None, str]:
        if not candidates:
            return None, None, "empty"
        try:
            probabilities = await self._probabilities(
                user_query=user_query,
                city=city,
                category=category,
                candidates=candidates,
                route_so_far=route_so_far,
            )
            ranked = align_probabilities(candidates, probabilities)
            source = "jev"
        except Exception as error:
            logger.warning("Jev недоступен, используется порядок RAG: %s", error)
            ranked = fallback_ranked(candidates)
            source = "fallback"
        primary, backup = select_pair(ranked)
        return primary, backup, source

    async def _probabilities(
        self,
        *,
        user_query: str,
        city: str,
        category: str,
        candidates: list[dict],
        route_so_far: list[dict],
    ) -> dict:
        payload = {
            "model": self.settings.jev_model,
            "state": {
                "user_query": user_query,
                "city": city,
                "category": category,
                "current_route": [
                    step["primary"]["name"]
                    for step in route_so_far
                    if step.get("primary")
                ],
            },
            "questions": {
                "best_poi": {
                    "type": "choice",
                    "instructions": JEV_INSTRUCTIONS,
                    "criteria": {
                        f"poi_{index}": candidate_criterion(candidate)
                        for index, candidate in enumerate(candidates)
                    },
                }
            },
        }
        headers = {
            "Authorization": f"Bearer {self.settings.routerai_api_key}",
            "Content-Type": "application/json",
        }
        if self.client is not None:
            response = await self.client.post(self.settings.decisions_url, headers=headers, json=payload)
            response.raise_for_status()
            return _read_probabilities(response.json())
        async with httpx.AsyncClient(timeout=30) as client:
            last_error = None
            for _ in range(3):
                try:
                    response = await client.post(
                        self.settings.decisions_url,
                        headers=headers,
                        json=payload,
                    )
                    response.raise_for_status()
                    return _read_probabilities(response.json())
                except (httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                    last_error = error
            raise RuntimeError(str(last_error))


def _read_probabilities(payload: dict) -> dict:
    answer = payload["answers"]["best_poi"]
    probabilities = answer.get("probabilities") or {}
    if not probabilities:
        raise ValueError("Jev не вернул вероятности")
    return probabilities
