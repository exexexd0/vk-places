import hashlib
import os
import re
import zipfile
from datetime import datetime
from pathlib import Path

from rag.settings import ROOT

SKIP_DIR_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
}
SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]{16,}")


def create_archive(root: Path = ROOT) -> Path:
    violations = find_disallowed_secrets(root)
    if violations:
        joined = ", ".join(violations)
        raise RuntimeError(f"Секрет найден вне .env: {joined}")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_dir = root / "artifacts" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    zip_path = archive_dir / f"ivanovo_rag_{stamp}.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in iter_project_files(root):
            archive.write(file_path, file_path.relative_to(root).as_posix())
    return zip_path


def iter_project_files(root: Path):
    for directory, dir_names, file_names in os.walk(root):
        current = Path(directory)
        dir_names[:] = [
            name
            for name in dir_names
            if not _skip_dir(current / name, root)
        ]
        for file_name in file_names:
            path = current / file_name
            if path.is_file():
                yield path


def find_disallowed_secrets(root: Path) -> list[str]:
    violations = []
    for file_path in iter_project_files(root):
        if file_path.name == ".env":
            continue
        text = file_path.read_bytes().decode("utf-8", errors="ignore")
        if SECRET_PATTERN.search(text):
            violations.append(file_path.relative_to(root).as_posix())
    return violations


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _skip_dir(path: Path, root: Path) -> bool:
    if path.name in SKIP_DIR_NAMES:
        return True
    archive_dir = root / "artifacts" / "archive"
    return path == archive_dir
