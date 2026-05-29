#!/usr/bin/env python3
"""One-shot file inventory builder for the CryptoBot 1.0 audit.

Walks the repo, skipping venv/git/cache/log/db directories, and emits
file_inventory.csv with columns: path, size_bytes, line_count, sha256, language, role.
"""
import csv
import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # ~/cryptobot
OUT = Path(__file__).resolve().parent / "file_inventory.csv"

EXCLUDE_DIRS = {
    "venv",
    ".git",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    "logs",
    "backups",
    "audit",  # don't inventory ourselves
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".so", ".dylib"}
EXCLUDE_GLOBS = ("cryptobot.db", "cryptobot.db-shm", "cryptobot.db-wal")

LANG_BY_EXT = {
    ".py": "python",
    ".md": "markdown",
    ".json": "json",
    ".sh": "shell",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".html": "html",
    ".css": "css",
    ".js": "javascript",
    ".ts": "typescript",
    ".toml": "toml",
    ".cfg": "ini",
    ".ini": "ini",
    ".txt": "text",
    ".env": "env",
    ".sql": "sql",
    ".csv": "csv",
}


def classify_role(p: Path, rel: str) -> str:
    name = p.name.lower()
    parts = rel.split("/")
    if parts[0] == "tests" or name.startswith("test_") or name.endswith("_test.py"):
        return "test"
    if parts[0] == "prompts":
        return "prompt"
    if parts[0] == "config":
        return "config"
    if parts[0] in {"docs"} or (p.suffix in {".md", ".docx", ".rst"} and parts[0] not in {"scalping_v2"}):
        # scalping_v2 has source AND docs; treat md as docs anyway
        return "docs"
    if p.suffix == ".md":
        return "docs"
    if parts[0] == "scripts" or p.suffix == ".sh":
        return "script"
    if parts[0] in {"data"} or p.suffix in {".db", ".sqlite", ".csv"}:
        return "data"
    if name in {"requirements.txt", "pytest.ini", "setup.py", "setup.cfg", "pyproject.toml"}:
        return "config"
    if name == "main.py" or parts[0] in {
        "core", "agents", "execution", "sentiment", "ui", "database",
        "data_sources", "macro", "predictive", "profiles", "signals",
        "strategies", "utils", "scalping_v2",
    }:
        return "source"
    if parts[0] == "alembic":
        return "migration"
    return "other"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def line_count(p: Path) -> int:
    try:
        with p.open("rb") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def main() -> None:
    rows = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        # mutate dirnames in-place to skip excluded dirs
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if any(fn.endswith(s) for s in EXCLUDE_SUFFIXES):
                continue
            if any(fn.startswith(g) for g in EXCLUDE_GLOBS):
                continue
            p = Path(dirpath) / fn
            try:
                rel = p.relative_to(ROOT).as_posix()
            except ValueError:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            lang = LANG_BY_EXT.get(p.suffix.lower(), "other")
            role = classify_role(p, rel)
            try:
                digest = sha256_file(p)
            except OSError:
                digest = ""
            lc = line_count(p) if size < 5 * 1024 * 1024 else 0
            rows.append((rel, size, lc, digest, lang, role))

    rows.sort(key=lambda r: r[0])
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["path", "size_bytes", "line_count", "sha256", "language", "role"])
        w.writerows(rows)
    print(f"{len(rows)} files inventoried -> {OUT}")


if __name__ == "__main__":
    main()
