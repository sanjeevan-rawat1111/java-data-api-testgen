"""
diff_reader.py — git diff-aware incremental analysis.

Detects which Java controller files changed in the target repo since the last
commit (or between two refs), then returns only the affected endpoints and
models so the LLM regenerates only what actually changed.
"""

import subprocess
from pathlib import Path

from testgen.analyzer.endpoint_parser import parse_endpoints
from testgen.analyzer.model_parser    import parse_models, models_to_text
from testgen.analyzer.schema_parser   import parse_schema, schema_to_text


def get_changed_java_files(repo_path: str, base_ref: str = "HEAD") -> list[str]:
    """
    Return list of changed .java file paths (relative to repo_path) vs base_ref.
    Includes staged, unstaged, and committed-but-not-pushed changes.
    """
    result = subprocess.run(
        ["git", "diff", base_ref, "--name-only", "--diff-filter=ACMRT", "--", "*.java"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    changed = [f.strip() for f in result.stdout.splitlines() if f.strip()]

    # Also catch unstaged changes
    result2 = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=ACMRT", "--", "*.java"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    unstaged = [f.strip() for f in result2.stdout.splitlines() if f.strip()]

    all_changed = list(dict.fromkeys(changed + unstaged))  # deduplicate, preserve order
    return all_changed


def read_diff_source(repo_path: str, base_ref: str = "HEAD") -> dict:
    """
    Like code_reader.read_source() but scoped to changed files only.

    Returns:
        {
            "controllers": [...],   # only changed controllers
            "models": [...],        # only changed models
            "schema_sql": str|None, # full schema (always included if schema changed)
            "changed_files": [...], # list of changed file paths
            "all_changed": bool,    # True if schema.sql changed (full regen suggested)
        }
    """
    repo = Path(repo_path).resolve()
    changed_files = get_changed_java_files(str(repo), base_ref)

    # Check if schema SQL changed
    sql_result = subprocess.run(
        ["git", "diff", base_ref, "--name-only", "--", "*.sql"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    sql_changed = bool(sql_result.stdout.strip())

    # Also check unstaged SQL
    sql_result2 = subprocess.run(
        ["git", "diff", "--name-only", "--", "*.sql"],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    sql_changed = sql_changed or bool(sql_result2.stdout.strip())

    controllers = []
    models = []

    for rel_path in changed_files:
        abs_path = repo / rel_path
        if not abs_path.exists():
            continue
        content = abs_path.read_text(encoding="utf-8", errors="ignore")

        # Classify as controller or model based on path / annotations
        if "controller" in rel_path.lower() or "Controller" in content:
            controllers.append({"file": rel_path, "content": content})
        elif "model" in rel_path.lower() or "entity" in rel_path.lower():
            models.append({"file": rel_path, "content": content})

    # Load schema SQL if it changed or if controllers changed (need table context)
    schema_sql = None
    if sql_changed or controllers:
        for sql_file in repo.rglob("schema.sql"):
            schema_sql = sql_file.read_text(encoding="utf-8", errors="ignore")
            break

    return {
        "controllers":    controllers,
        "models":         models,
        "schema_sql":     schema_sql,
        "changed_files":  changed_files,
        "schema_changed": sql_changed,
    }


def build_diff_context(diff_source: dict) -> str:
    """Build the LLM user message from diff source (same format as full context)."""
    parts = []

    if diff_source["controllers"]:
        parts.append("=== CHANGED REST ENDPOINTS ===")
        parts.append("Generate or update tests ONLY for these endpoints:")
        for ctrl in diff_source["controllers"]:
            for ep in parse_endpoints(ctrl["content"]):
                line = f"{ep['method']:7} {ep['path']}"
                if ep["description"]:
                    line += f"\n  description: {ep['description']}"
                for p in ep["params"]:
                    line += f"\n  param [{p['source']}]: {p['name']} ({p['type']})"
                if ep["request_body"]:
                    line += f"\n  body: {ep['request_body']}"
                parts.append(line)

    if diff_source["models"]:
        parts.append("\n=== CHANGED DATA MODELS ===")
        models = parse_models(diff_source["models"])
        parts.append(models_to_text(models))

    if diff_source["schema_sql"]:
        parts.append("=== DATABASE SCHEMA (for context) ===")
        parts.append(schema_to_text(parse_schema(diff_source["schema_sql"])))

    return "\n".join(parts)
