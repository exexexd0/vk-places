from rag.archive import find_disallowed_secrets, iter_project_files


def test_secret_scan_allows_only_env_file(tmp_path):
    leaked = _key("leakedsecretvalue123")
    allowed = _key("allowedsecretvalue123")
    hidden = _key("gitsecretvalue1234567")
    (tmp_path / ".env").write_text(f"ROUTERAI_API_KEY={allowed}", encoding="utf-8")
    (tmp_path / "rag").mkdir()
    (tmp_path / "rag" / "config.py").write_text(f"key = '{leaked}'", encoding="utf-8")
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(hidden, encoding="utf-8")

    violations = find_disallowed_secrets(tmp_path)
    files = {path.relative_to(tmp_path).as_posix() for path in iter_project_files(tmp_path)}

    assert violations == ["rag/config.py"]
    assert ".git/config" not in files
    assert ".env" in files


def _key(body: str) -> str:
    return "sk-" + body
