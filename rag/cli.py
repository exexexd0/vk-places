import argparse
import asyncio
import json
import logging
from datetime import datetime, timezone
from importlib.metadata import version

from rag.archive import create_archive, file_sha256
from rag.graph import build_route_graph, initial_state, route_view, validate_route
from rag.jev import JevSelector
from rag.normalize import load_places
from rag.planner import LlmPlanner
from rag.routing import OsrmRouter
from rag.settings import ROOT, load_settings
from rag.store import available_categories, open_store, rebuild_index

RUNS = ROOT / "artifacts" / "runs"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="RAG-маршрут по точкам Иваново")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("report")
    commands.add_parser("rebuild")
    route = commands.add_parser("route")
    route.add_argument("--query", required=True)
    route.add_argument("--city")
    route.add_argument("--transport-mode", choices=["walk", "car", "transit"], default="walk")
    commands.add_parser("archive")
    args = parser.parse_args()
    if args.command == "report":
        write_report()
    elif args.command == "rebuild":
        asyncio.run(rebuild())
    elif args.command == "route":
        asyncio.run(generate(args.query, args.city, args.transport_mode))
    elif args.command == "archive":
        archive()


def write_report() -> dict:
    settings = load_settings()
    result = load_places(settings.source_path, settings.city)
    payload = {
        "source": str(settings.source_path),
        "source_count": result.source_count,
        "kept": len(result.places),
        "dropped": result.dropped,
        "by_category": result.by_category,
    }
    _write_json("category_report.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


async def rebuild() -> dict:
    settings = load_settings()
    result = load_places(settings.source_path, settings.city)
    report = {
        "source_count": result.source_count,
        "kept": len(result.places),
        "dropped": result.dropped,
        "by_category": result.by_category,
    }
    _write_json("category_report.json", report)
    verified = await rebuild_index(settings, result.places)
    report["index"] = verified
    _write_json("category_report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report


async def generate(user_query: str, city: str | None, transport_mode: str = "walk") -> dict:
    settings = load_settings()
    selected_city = city or settings.city
    store = await open_store(settings)
    categories = available_categories(settings)
    app = build_route_graph(
        LlmPlanner(settings),
        store,
        JevSelector(settings),
        router=OsrmRouter(settings.osrm_base_url),
    )
    state = await app.ainvoke(
        initial_state(user_query, selected_city, categories, transport_mode=transport_mode)
    )
    payload = route_view(state)
    validate_route(payload)
    _write_json("route.json", payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def archive() -> None:
    route_path = RUNS / "route.json"
    report_path = RUNS / "category_report.json"
    route = json.loads(route_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    validate_route(route)
    settings = load_settings()
    sources = sorted({step.get("selection_source") for step in route["steps"]})
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "embedding_model": settings.embedding_model,
        "embedding_dim": settings.embedding_dim,
        "chat_model": settings.chat_model,
        "jev_model": settings.jev_model,
        "prompts_file": "rag/prompts.py",
        "source_sha256": file_sha256(settings.source_path),
        "source_count": report.get("source_count"),
        "kept": report.get("kept"),
        "by_category": report.get("by_category"),
        "index": report.get("index"),
        "selection_sources": sources,
        "env_included": True,
        "dependency_versions": {
            name: version(name)
            for name in (
                "langchain-postgres",
                "langchain-openai",
                "langgraph",
                "pgvector",
            )
        },
    }
    _write_json("manifest.json", manifest)
    zip_path = create_archive()
    print(zip_path)


def _write_json(name: str, payload: dict) -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    (RUNS / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
