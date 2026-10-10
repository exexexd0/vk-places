import json
import logging
import re

import psycopg
from langchain_postgres import PGEngine
from langchain_postgres.v2.engine import Column
from langchain_postgres.v2.indexes import HNSWIndex
from langchain_postgres.v2.vectorstores import PGVectorStore

from rag.documents import places_to_documents
from rag.embeddings import assert_embedding_dimension, build_embeddings
from rag.normalize import Place
from rag.prompts import retrieval_query
from rag.selection import confidence_from_distance
from rag.settings import Settings

logger = logging.getLogger("rag.store")

METADATA_COLUMNS = [
    Column("org_id", "TEXT", nullable=False),
    Column("city", "TEXT", nullable=False),
    Column("category", "TEXT", nullable=False),
    Column("name", "TEXT", nullable=False),
    Column("address", "TEXT"),
    Column("type", "TEXT"),
    Column("rating", "DOUBLE PRECISION"),
    Column("reviews", "INTEGER"),
    Column("lat", "DOUBLE PRECISION", nullable=False),
    Column("lon", "DOUBLE PRECISION", nullable=False),
]
METADATA_COLUMN_NAMES = [column.name for column in METADATA_COLUMNS]
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class PlaceStore:
    def __init__(self, settings: Settings, vector_store: PGVectorStore):
        self.settings = settings
        self.vector_store = vector_store

    async def search(
        self,
        *,
        user_query: str,
        city: str,
        category: str,
        excluded_org_ids: list[str],
        previous: dict | None,
    ) -> list[dict]:
        query = retrieval_query(user_query, category, previous)
        rows = await self.vector_store.asimilarity_search_with_score(
            query,
            k=self.settings.top_k + len(excluded_org_ids),
            filter={"city": city, "category": category},
        )
        excluded = set(excluded_org_ids)
        found = []
        for document, distance in rows:
            org_id = str(document.metadata.get("org_id") or "")
            if not org_id or org_id in excluded:
                continue
            found.append(_candidate(document.metadata, float(distance)))
            if len(found) == self.settings.top_k:
                break
        return found


async def rebuild_index(settings: Settings, places: list[Place]) -> dict:
    embeddings = build_embeddings(settings)
    await assert_embedding_dimension(embeddings, settings.embedding_dim)
    _create_extension(settings)
    engine = PGEngine.from_connection_string(settings.database_url)
    await engine.ainit_vectorstore_table(
        table_name=settings.vector_table,
        vector_size=settings.embedding_dim,
        metadata_columns=METADATA_COLUMNS,
        metadata_json_column="langchain_metadata",
        id_column=Column("langchain_id", "TEXT", nullable=False),
        overwrite_existing=True,
    )
    store = await PGVectorStore.create(
        engine=engine,
        embedding_service=embeddings,
        table_name=settings.vector_table,
        metadata_columns=METADATA_COLUMN_NAMES,
        metadata_json_column="langchain_metadata",
        id_column="langchain_id",
    )
    documents = places_to_documents(places)
    ids = [place.org_id for place in places]
    batch_size = settings.batch_size
    for start in range(0, len(documents), batch_size):
        end = start + batch_size
        logger.info("Запись эмбеддингов %s-%s из %s", start + 1, min(end, len(documents)), len(documents))
        await store.aadd_documents(documents[start:end], ids=ids[start:end])
    await store.aapply_vector_index(HNSWIndex(), name=f"{settings.vector_table}_embedding_hnsw")
    report = verify_index(settings)
    return report


async def open_store(settings: Settings) -> PlaceStore:
    embeddings = build_embeddings(settings)
    engine = PGEngine.from_connection_string(settings.database_url)
    store = await PGVectorStore.create(
        engine=engine,
        embedding_service=embeddings,
        table_name=settings.vector_table,
        metadata_columns=METADATA_COLUMN_NAMES,
        metadata_json_column="langchain_metadata",
        id_column="langchain_id",
    )
    return PlaceStore(settings, store)


def verify_index(settings: Settings) -> dict:
    table = _identifier(settings.vector_table)
    with psycopg.connect(settings.sync_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*), COUNT(DISTINCT org_id) FROM {table}")
            total, distinct_ids = cursor.fetchone()
            cursor.execute(f"SELECT vector_dims(embedding) FROM {table} LIMIT 1")
            dimension_row = cursor.fetchone()
            cursor.execute(
                f"""
                SELECT COUNT(*) FROM {table}
                WHERE city IS NULL OR category IS NULL OR lat IS NULL OR lon IS NULL
                   OR langchain_id <> org_id
                """
            )
            invalid = cursor.fetchone()[0]
            cursor.execute(f"SELECT category, COUNT(*) FROM {table} GROUP BY category ORDER BY category")
            by_category = {category: count for category, count in cursor.fetchall()}
            cursor.execute(f"SELECT langchain_metadata::text FROM {table}")
            metadata_rows = cursor.fetchall()
    if total == 0:
        raise RuntimeError("Векторная таблица пуста")
    dimension = dimension_row[0]
    if dimension != settings.embedding_dim:
        raise RuntimeError(f"В таблице vector({dimension}), ожидалось vector({settings.embedding_dim})")
    if distinct_ids != total or invalid:
        raise RuntimeError("В индексе есть пустые координаты, пустые категории или повторы org_id")
    for (payload_text,) in metadata_rows:
        payload = json.loads(payload_text or "{}")
        if "comments" in payload:
            raise RuntimeError("В metadata попали отзывы")
        for field in ("city", "category", "lat", "lon", "org_id"):
            if field not in payload and field not in METADATA_COLUMN_NAMES:
                raise RuntimeError(f"У записи нет поля {field}")
    return {
        "count": total,
        "embedding_dim": dimension,
        "by_category": by_category,
    }


def available_categories(settings: Settings) -> list[str]:
    table = _identifier(settings.vector_table)
    with psycopg.connect(settings.sync_database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f"SELECT DISTINCT category FROM {table} ORDER BY category")
            return [row[0] for row in cursor.fetchall()]


def _create_extension(settings: Settings) -> None:
    with psycopg.connect(settings.sync_database_url, autocommit=True) as connection:
        connection.execute("CREATE EXTENSION IF NOT EXISTS vector")


def _identifier(name: str) -> str:
    if not IDENTIFIER.fullmatch(name):
        raise ValueError(f"Недопустимое имя таблицы: {name}")
    return name


def _candidate(metadata: dict, distance: float) -> dict:
    return {
        "org_id": metadata.get("org_id"),
        "name": metadata.get("name"),
        "address": metadata.get("address"),
        "category": metadata.get("category"),
        "type": metadata.get("type"),
        "city": metadata.get("city"),
        "lat": metadata.get("lat"),
        "lon": metadata.get("lon"),
        "rating": metadata.get("rating"),
        "phone": metadata.get("phone"),
        "url": metadata.get("url"),
        "website": metadata.get("website"),
        "reviews": metadata.get("reviews"),
        "opening_hours": metadata.get("opening_hours"),
        "distance": distance,
        "confidence": confidence_from_distance(distance),
    }
