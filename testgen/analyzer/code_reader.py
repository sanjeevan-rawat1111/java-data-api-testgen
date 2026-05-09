"""
code_reader.py — reads the java-data-api source tree into a structured dict.

Uses a ThreadPoolExecutor to read all Java files in parallel, significantly
reducing I/O time on large codebases with many source files.

Output shape:
{
  "controllers": [{"filename": str, "content": str}, ...],
  "models":      {"ClassName": "<java source>", ...},
  "schema_sql":  "<full SQL text>",
  "source_root": "/path/to/java-data-api"
}
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

logger = logging.getLogger(__name__)

CONTROLLER_GLOB = "src/main/java/**/*Controller*.java"
MODEL_GLOB      = "src/main/java/**/*.java"
SCHEMA_PATH     = "src/main/resources/sql/schema.sql"

MODEL_KEYWORDS  = ("Request", "Response", "User")
_SKIP_DIRS      = {"config", "repository", "service"}

# Max parallel file-read workers — keeps OS file-descriptor usage bounded
_MAX_WORKERS = 16


def _read_file(path: Path) -> tuple[Path, str]:
    """Read a single file and return (path, content). Safe for thread pool use."""
    try:
        return path, path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("cannot read %s: %s", path, exc)
        return path, ""


def read_source(repo_root: str) -> dict:
    """
    Read all Java source files from *repo_root* in parallel and return
    structured source data for the prompt builder.
    """
    root = Path(repo_root)
    if not root.exists():
        raise FileNotFoundError(f"java-data-api repo not found at: {repo_root}")

    # ── Discover files ────────────────────────────────────────────────────────
    controller_paths = sorted(root.glob(CONTROLLER_GLOB))

    model_paths: list[Path] = []
    for path in root.glob(MODEL_GLOB):
        if any(d in path.parts for d in _SKIP_DIRS):
            continue
        if "Controller" in path.name:
            continue
        if any(kw in path.name for kw in MODEL_KEYWORDS):
            model_paths.append(path)

    schema_path = root / SCHEMA_PATH
    all_paths = controller_paths + model_paths + (
        [schema_path] if schema_path.exists() else []
    )

    if not all_paths:
        logger.warning("no Java files found under %s", root)
        return {
            "source_root": str(root),
            "controllers": [],
            "models": {},
            "schema_sql": "",
        }

    # ── Read all files in parallel ─────────────────────────────────────────
    workers = min(_MAX_WORKERS, len(all_paths))
    logger.debug("reading %d files with %d workers", len(all_paths), workers)

    contents: dict[Path, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_read_file, p): p for p in all_paths}
        for future in as_completed(futures):
            path, content = future.result()
            contents[path] = content

    # ── Assemble result (preserve discovery order) ─────────────────────────
    controllers = [
        {"filename": p.name, "content": contents[p]}
        for p in controller_paths
        if contents.get(p)
    ]

    models = {
        p.stem: contents[p]
        for p in model_paths
        if contents.get(p)
    }

    schema_sql = contents.get(schema_path, "") if schema_path.exists() else ""

    logger.debug(
        "read complete: %d controllers, %d models, schema=%s",
        len(controllers), len(models), "yes" if schema_sql else "no",
    )

    return {
        "source_root": str(root),
        "controllers": controllers,
        "models": models,
        "schema_sql": schema_sql,
    }
