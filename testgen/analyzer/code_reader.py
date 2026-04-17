"""
code_reader.py — reads the java-data-api source tree into a structured dict.

Output shape:
{
  "controllers": [<file content>, ...],
  "models":      {"ClassName": <file content>, ...},
  "schema_sql":  "<full SQL text>",
  "source_root": "/path/to/java-data-api"
}
"""

import os
from pathlib import Path


CONTROLLER_GLOB = "src/main/java/**/*Controller*.java"
MODEL_GLOB      = "src/main/java/**/*.java"
SCHEMA_PATH     = "src/main/resources/sql/schema.sql"

# model classes we care about (skip controllers/config/repos/services)
MODEL_KEYWORDS  = ("Request", "Response", "User")


def read_source(repo_root: str) -> dict:
    root = Path(repo_root)
    if not root.exists():
        raise FileNotFoundError(f"java-data-api repo not found at: {repo_root}")

    result = {
        "source_root": str(root),
        "controllers": [],
        "models": {},
        "schema_sql": "",
    }

    # Controllers
    for path in root.glob(CONTROLLER_GLOB):
        result["controllers"].append({
            "filename": path.name,
            "content": path.read_text(encoding="utf-8"),
        })

    # Models (filter by keyword, skip infra classes)
    skip_dirs = {"config", "repository", "service"}
    for path in root.glob(MODEL_GLOB):
        if any(d in path.parts for d in skip_dirs):
            continue
        if "Controller" in path.name:
            continue
        if any(kw in path.name for kw in MODEL_KEYWORDS):
            result["models"][path.stem] = path.read_text(encoding="utf-8")

    # SQL schema
    schema_path = root / SCHEMA_PATH
    if schema_path.exists():
        result["schema_sql"] = schema_path.read_text(encoding="utf-8")

    return result
