import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    database_url: str
    routerai_api_key: str
    routerai_base_url: str
    embedding_model: str
    embedding_dim: int
    chat_model: str
    jev_model: str
    vector_table: str
    batch_size: int
    top_k: int
    city: str
    source_path: Path
    osrm_base_url: str

    @property
    def sync_database_url(self) -> str:
        return (
            self.database_url.replace("postgresql+asyncpg://", "postgresql://")
            .replace("postgresql+psycopg://", "postgresql://")
        )

    @property
    def decisions_url(self) -> str:
        return self.routerai_base_url.rstrip("/") + "/decisions"


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env")
    source = Path(os.environ.get("SOURCE_PATH", "Иваново.json"))
    if not source.is_absolute():
        source = ROOT / source
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        routerai_api_key=os.environ["ROUTERAI_API_KEY"],
        routerai_base_url=os.environ.get("ROUTERAI_BASE_URL", "https://routerai.ru/api/v1"),
        embedding_model=os.environ.get("EMBEDDING_MODEL", "baai/bge-m3"),
        embedding_dim=int(os.environ.get("EMBEDDING_DIM", "1024")),
        chat_model=os.environ.get("CHAT_MODEL", "openai/gpt-4o-mini"),
        jev_model=os.environ.get("JEV_MODEL", "~typesafe/jev-latest"),
        vector_table=os.environ.get("VECTOR_TABLE", "ivanovo_places"),
        batch_size=int(os.environ.get("BATCH_SIZE", "8")),
        top_k=int(os.environ.get("TOP_K", "5")),
        city=os.environ.get("CITY", "Иваново"),
        source_path=source,
        osrm_base_url=os.environ.get("OSRM_BASE_URL", "http://127.0.0.1:5000"),
    )
