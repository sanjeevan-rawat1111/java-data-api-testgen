"""
newman_runner.py — Python wrapper around Newman for programmatic test execution.

Runs a Postman collection, captures structured results (pass/fail/failures with
response bodies) so the LLM debug loop can analyze exactly what went wrong.
"""

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TestFailure:
    test:          str
    request:       str
    message:       str
    response_body: str | None = None

    def describe(self) -> str:
        lines = [
            f"  Request : {self.request}",
            f"  Test    : {self.test}",
            f"  Error   : {self.message}",
        ]
        if self.response_body:
            lines.append(f"  Response: {self.response_body[:300]}")
        return "\n".join(lines)


@dataclass
class RunResult:
    collection_name: str
    total:    int
    passed:   int
    failed:   int
    requests: int
    report_path: str
    failures: list[TestFailure] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        icon = "✔" if self.success else "✖"
        return (f"{icon} {self.collection_name} — "
                f"{self.passed}/{self.total} assertions passed, "
                f"{self.requests} requests")


def _ensure_npm_deps(runner_dir: str):
    """Run npm install if node_modules is missing."""
    nm = Path(runner_dir) / "node_modules"
    if not nm.exists():
        print("      Installing Newman dependencies (npm install)...")
        subprocess.run(["npm", "install"], cwd=runner_dir, check=True, capture_output=True)


def run_collection(
    collection_path: str,
    env_path: str,
    runner_dir: str = "runner",
) -> RunResult:
    """
    Run a single Postman collection via Node Newman and return structured results.
    """
    runner_dir = str(Path(runner_dir).resolve())
    _ensure_npm_deps(runner_dir)

    # Write results to a temp JSON file so we can parse failures with response bodies
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
        json_out = tf.name

    try:
        proc = subprocess.run(
            [
                "node", "src/cli.js",
                "--collection", Path(collection_path).stem,
                "--json-out", json_out,
            ],
            cwd=runner_dir,
            capture_output=False,
            env={**os.environ, "TESTGEN_JSON_OUT": json_out},
        )

        # Parse the JSON results if written
        if Path(json_out).exists() and Path(json_out).stat().st_size > 0:
            with open(json_out) as f:
                data = json.load(f)
            return _parse_json_results(data)

        # Fallback: return a minimal result based on exit code
        return RunResult(
            collection_name=Path(collection_path).stem,
            total=0, passed=0,
            failed=0 if proc.returncode == 0 else 1,
            requests=0,
            report_path="",
            failures=[] if proc.returncode == 0 else [
                TestFailure(test="Newman execution", request="N/A",
                            message=f"Newman exited with code {proc.returncode}")
            ],
        )
    finally:
        try:
            os.unlink(json_out)
        except OSError:
            pass


def run_all_collections(
    collections_dir: str = "collections",
    env_path: str = "env/environment.json",
    runner_dir: str = "runner",
) -> list[RunResult]:
    """Run all collections in collections_dir and return results list."""
    runner_dir_resolved = str(Path(runner_dir).resolve())
    _ensure_npm_deps(runner_dir_resolved)

    collections = sorted(Path(collections_dir).glob("*.json"))
    if not collections:
        return []

    results = []
    for col in collections:
        print(f"  ▶  Running: {col.name}")
        result = run_collection(str(col), env_path, runner_dir)
        print(f"  {result.summary()}")
        if result.failures:
            for f in result.failures:
                print(f.describe())
        results.append(result)

    return results


def _parse_json_results(data: dict) -> RunResult:
    """Parse Newman JSON reporter output into RunResult."""
    run   = data.get("run", {})
    stats = run.get("stats", {})
    assertions = stats.get("assertions", {})
    requests   = stats.get("requests",   {})

    failures = []
    for f in run.get("failures", []):
        err  = f.get("error", {})
        src  = f.get("source", {})
        exec_data = f.get("at", {})
        resp_body = None
        try:
            resp_body = exec_data.get("response", {}).get("body", "")[:500]
        except Exception:
            pass
        failures.append(TestFailure(
            test=err.get("test", "unknown"),
            request=src.get("name", "unknown"),
            message=err.get("message", ""),
            response_body=resp_body,
        ))

    # Find most recent HTML report
    reports = sorted(Path("runner/reports").glob("*.html"), key=lambda p: p.stat().st_mtime)
    report_path = str(reports[-1]) if reports else ""

    total  = assertions.get("total", 0)
    failed = assertions.get("failed", 0)
    return RunResult(
        collection_name=data.get("collection", {}).get("info", {}).get("name", "unknown"),
        total=total,
        passed=total - failed,
        failed=failed,
        requests=requests.get("total", 0),
        report_path=report_path,
        failures=failures,
    )
