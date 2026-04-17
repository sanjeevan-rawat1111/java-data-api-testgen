"""
cli.py — command-line entry point for testgen.

Usage:
  python -m testgen run                             # full pipeline: generate → start services → execute → report
  python -m testgen run --skip-generate            # reuse existing collection, just run tests
  python -m testgen generate                        # generate full suite only
  python -m testgen generate --feature UsersCRUD   # custom output name
  python -m testgen generate --dry-run             # show prompt, skip LLM call
  python -m testgen generate --diff                # only regenerate changed endpoints
  python -m testgen generate --diff --base main    # diff against specific branch/ref
  python -m testgen validate collections/X.json   # validate a collection
  python -m testgen analyze                        # print analyzed API context
  python -m testgen analyze --diff                 # show only changed endpoints
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml

from testgen.analyzer.code_reader      import read_source
from testgen.analyzer.diff_reader      import read_diff_source, build_diff_context
from testgen.analyzer.schema_parser    import parse_schema, schema_to_text
from testgen.analyzer.model_parser     import parse_models, models_to_text
from testgen.generator.prompt_builder     import build_context, SYSTEM_PROMPT
from testgen.generator.llm_client         import LLMClient
from testgen.generator.collection_builder import build_and_save_with_healing, merge_collections, write_collection
from testgen.generator.debug_builder      import build_debug_prompt
from testgen.runner.newman_runner         import run_all_collections
from testgen.validator.validate           import load_collection, validate


# ── Config loading ─────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        print(f"[warn] config.yaml not found at {path}, using defaults")
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


# ── Subcommands ───────────────────────────────────────────────────────────────

def cmd_generate(args, config: dict):
    repo_root  = config.get("java_data_api_path", "../java-data-api")
    output_dir = config.get("output_dir", "collections")
    feature    = args.feature or config.get("default_feature_name", "java-data-api-tests")
    diff_mode  = getattr(args, "diff", False)
    base_ref   = getattr(args, "base", "HEAD")

    if diff_mode:
        _cmd_generate_diff(args, config, repo_root, output_dir, feature, base_ref)
    else:
        _cmd_generate_full(args, config, repo_root, output_dir, feature)


def _cmd_generate_full(args, config, repo_root, output_dir, feature):
    print(f"[1/3] Reading source from: {repo_root}")
    source_data = read_source(repo_root)
    print(f"      Controllers : {len(source_data['controllers'])}")
    print(f"      Models      : {len(source_data['models'])}")
    print(f"      Schema SQL  : {'yes' if source_data['schema_sql'] else 'no'}")

    print("[2/3] Building prompt context...")
    user_message = build_context(source_data)
    token_est    = LLMClient.estimate_tokens(SYSTEM_PROMPT + user_message)
    print(f"      Estimated tokens: ~{token_est:,}")

    if args.dry_run:
        print("\n── SYSTEM PROMPT ──────────────────────────────────────────")
        print(SYSTEM_PROMPT[:500] + "...")
        print("\n── USER MESSAGE ───────────────────────────────────────────")
        print(user_message[:2000] + ("..." if len(user_message) > 2000 else ""))
        print("\n[dry-run] Skipping LLM call.")
        return

    _call_llm_and_save(config, user_message, feature, output_dir)


def _cmd_generate_diff(args, config, repo_root, output_dir, feature, base_ref):
    print(f"[1/3] Detecting changes in: {repo_root}  (vs {base_ref})")
    diff_source = read_diff_source(repo_root, base_ref)

    changed = diff_source["changed_files"]
    if not changed:
        print("      No Java changes detected — nothing to regenerate.")
        print("      Tip: use 'generate' (without --diff) to regenerate the full suite.")
        return

    print(f"      Changed files : {len(changed)}")
    for f in changed:
        print(f"        • {f}")
    if diff_source["schema_changed"]:
        print("      ⚠ schema.sql changed — DB context included in prompt")

    if not diff_source["controllers"]:
        print("      No controller changes detected — no endpoint tests to regenerate.")
        return

    print("[2/3] Building diff prompt context...")
    user_message = build_diff_context(diff_source)
    token_est    = LLMClient.estimate_tokens(SYSTEM_PROMPT + user_message)
    print(f"      Estimated tokens: ~{token_est:,}  (vs full suite)")

    if args.dry_run:
        print("\n── SYSTEM PROMPT ──────────────────────────────────────────")
        print(SYSTEM_PROMPT[:500] + "...")
        print("\n── USER MESSAGE (diff) ────────────────────────────────────")
        print(user_message[:2000] + ("..." if len(user_message) > 2000 else ""))
        print("\n[dry-run] Skipping LLM call.")
        return

    # Generate a temporary collection for the changed endpoints
    temp_feature = f"{feature}_diff_patch"
    result = _call_llm_and_save(config, user_message, temp_feature, output_dir, silent=True)
    if not result:
        return

    # Merge patch into existing collection (if it exists)
    existing_path = Path(output_dir) / f"{feature}.json"
    patch_path    = Path(result["output_path"])

    if existing_path.exists():
        print(f"[3/3] Merging patch into existing collection: {existing_path.name}")
        merged = merge_collections(
            base_path=str(existing_path),
            patch_path=str(patch_path),
        )
        with open(existing_path, "w") as f:
            json.dump(merged, f, indent=2)
        patch_path.unlink()  # remove temp patch file
        print(f"\n✔ Collection updated: {existing_path}")
        print(f"  Endpoints updated : {len(diff_source['changed_files'])}")
    else:
        # No existing collection — rename patch to main collection
        patch_path.rename(existing_path)
        print(f"\n✔ Collection created: {existing_path}")

    print(f"\n→  Run tests:  docker-compose --profile run up newman-runner")


def _call_llm_and_save(config, user_message, feature, output_dir, silent=False):
    max_retries = config.get("max_retries", 3)
    if not silent:
        print(f"[3/3] Calling LLM ({config.get('provider','openai')} / {config.get('model','gemini-1.5-flash')}) "
              f"— self-healing enabled (max {max_retries} attempts)...")
    else:
        print(f"[3/3] Calling LLM for changed endpoints ({max_retries} max attempts)...")

    result = build_and_save_with_healing(
        llm_client=LLMClient(config),
        system_prompt=SYSTEM_PROMPT,
        user_message=user_message,
        feature_name=feature,
        output_dir=output_dir,
    )

    if not silent:
        healed = " (self-healed)" if result.get("self_healed") else ""
        print(f"\n✔ Collection saved to: {result['output_path']}{healed}")
        print(f"  Requests generated : {result['total_requests']}")
        print(f"  LLM attempts used  : {result['attempts']}/{config.get('max_retries', 3)}")
        print(f"\n→  Run tests:  docker-compose --profile run up newman-runner")
        if result["warnings"]:
            for w in result["warnings"]:
                print(f"    ⚠ {w}")

    return result


def cmd_run(args, config: dict):
    """
    Full agentic pipeline:
      1. Generate collection (LLM)
      2. Start services (docker-compose)
      3. Run tests (Newman)
      4. If failures → send to LLM to fix → re-run  (up to max_debug_attempts)
      5. Save final collection + open HTML report
    """
    feature           = args.feature or config.get("default_feature_name", "java-data-api-tests")
    output_dir        = config.get("output_dir", "collections")
    skip_gen          = getattr(args, "skip_generate", False)
    max_debug         = config.get("max_debug_attempts", 3)
    wait_secs         = config.get("service_wait_seconds", 25)

    _banner("java-data-api-testgen  —  generate → run → self-debug pipeline")

    # ── Step 1: Generate ──────────────────────────────────────────────────────
    if skip_gen:
        collections = list(Path(output_dir).glob("*.json"))
        if not collections:
            print("✖ No collections found in collections/. Run without --skip-generate first.")
            sys.exit(1)
        print(f"[1/4] Skipping generation — using {len(collections)} existing collection(s)")
    else:
        print("[1/4] Generating test collection via LLM...")
        class _GenArgs:
            dry_run = False
            diff    = False
            base    = "HEAD"
        fake_args         = _GenArgs()
        fake_args.feature = feature
        cmd_generate(fake_args, config)
        print()

    # ── Step 2: Start services ────────────────────────────────────────────────
    print("[2/4] Starting services (docker-compose)...")
    svc_result = subprocess.run(
        ["docker-compose", "up", "-d", "mysql", "aerospike", "java-data-api", "flask-helper"],
        capture_output=False,
    )
    if svc_result.returncode != 0:
        print("✖ docker-compose failed. Is Docker running?")
        sys.exit(1)

    print(f"      Waiting {wait_secs}s for services to be ready...", end="", flush=True)
    for _ in range(wait_secs):
        time.sleep(1)
        print(".", end="", flush=True)
    print(" ready\n")

    # ── Step 3 + 4: Run → debug loop ─────────────────────────────────────────
    llm_client   = LLMClient(config)
    total_rounds = max_debug + 1  # initial run + N debug rounds
    all_passed   = False

    for attempt in range(total_rounds):
        is_first = attempt == 0
        label    = "[3/4] Running tests (Newman)..." if is_first else \
                   f"[3/4] Re-running tests after LLM fix (attempt {attempt}/{max_debug})..."
        print(label)

        results = run_all_collections(
            collections_dir=output_dir,
            env_path="env/environment.json",
            runner_dir="runner",
        )

        total_failed = sum(r.failed  for r in results)
        total_passed = sum(r.passed  for r in results)
        total_tests  = sum(r.total   for r in results)

        print(f"\n      Results: {total_passed}/{total_tests} passed, {total_failed} failed")

        if total_failed == 0:
            all_passed = True
            break

        if attempt == max_debug:
            print(f"\n  ⚠ Reached max debug attempts ({max_debug}). Saving best collection.")
            break

        # ── LLM debug: fix failing tests ─────────────────────────────────────
        all_failures = [f for r in results for f in r.failures]
        print(f"\n[4/4] {len(all_failures)} test(s) failed — asking LLM to fix (debug round {attempt + 1}/{max_debug})...")

        for col_path in Path(output_dir).glob("*.json"):
            with open(col_path) as f:
                collection = json.load(f)

            col_failures = [f for r in results if r.collection_name in col_path.stem for f in r.failures]
            if not col_failures:
                continue

            system_prompt, user_msg = build_debug_prompt(collection, col_failures)

            raw, attempts_used = llm_client.generate_with_healing(
                system_prompt=system_prompt,
                user_message=user_msg,
            )
            try:
                fixed = json.loads(raw)
                with open(col_path, "w") as f:
                    json.dump(fixed, f, indent=2)
                healed = " (self-healed)" if attempts_used > 1 else ""
                print(f"  ✔ Fixed collection saved: {col_path.name}{healed}")
            except json.JSONDecodeError as e:
                print(f"  ✖ LLM returned invalid JSON for {col_path.name}: {e}")

        print()

    # ── Summary ───────────────────────────────────────────────────────────────
    _banner("Pipeline complete")
    status = "ALL TESTS PASSED ✔" if all_passed else f"FINISHED WITH FAILURES ✖  (check report)"
    print(f"  Status  : {status}")

    reports = sorted(Path("runner/reports").glob("*.html"), key=lambda p: p.stat().st_mtime)
    if reports:
        latest = reports[-1]
        print(f"  Report  : {latest}")
        subprocess.run(["open", str(latest)], capture_output=True)

    sys.exit(0 if all_passed else 1)


def _banner(text: str):
    width = max(len(text) + 4, 60)
    print("─" * width)
    print(f"  {text}")
    print("─" * width)


def cmd_validate(args, config: dict):
    path = args.path
    try:
        col    = load_collection(path)
        result = validate(col)
    except Exception as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    status = "✔ VALID" if result["valid"] else "✖ INVALID"
    print(f"\n{status}  {path}")
    print(f"  Requests: {result['stats'].get('requests', 0)}")
    print(f"  Tested  : {result['stats'].get('tested', 0)}")
    if result["errors"]:
        print("\nErrors:")
        for e in result["errors"]:
            print(f"  ✖ {e}")
    if result["warnings"]:
        print("\nWarnings:")
        for w in result["warnings"]:
            print(f"  ⚠ {w}")
    sys.exit(0 if result["valid"] else 1)


def cmd_analyze(args, config: dict):
    repo_root  = config.get("java_data_api_path", "../java-data-api")
    diff_mode  = getattr(args, "diff", False)
    base_ref   = getattr(args, "base", "HEAD")

    from testgen.analyzer.endpoint_parser import parse_endpoints

    if diff_mode:
        diff_source = read_diff_source(repo_root, base_ref)
        changed = diff_source["changed_files"]
        if not changed:
            print("No changes detected.")
            return
        print(f"\n── Changed files ({len(changed)}) ─────────────────────────────")
        for f in changed:
            print(f"  • {f}")
        print("\n── Changed endpoints ──────────────────────────────────────")
        for ctrl in diff_source["controllers"]:
            for ep in parse_endpoints(ctrl["content"]):
                print(f"  {ep['method']:7} {ep['path']}")
        return

    source_data = read_source(repo_root)
    print("\n── Endpoints ──────────────────────────────────────────────")
    for ctrl in source_data["controllers"]:
        eps = parse_endpoints(ctrl["content"])
        for ep in eps:
            print(f"  {ep['method']:7} {ep['path']}")
            if ep["request_body"]:
                print(f"           body: {ep['request_body']}")

    print("\n── Models ─────────────────────────────────────────────────")
    models = parse_models(source_data["models"])
    print(models_to_text(models))

    if source_data["schema_sql"]:
        print("── DB Schema ──────────────────────────────────────────────")
        print(schema_to_text(parse_schema(source_data["schema_sql"])))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="testgen",
        description="AI-powered Postman test generator for java-data-api",
    )
    sub = parser.add_subparsers(dest="command")

    # run  (full pipeline)
    run_p = sub.add_parser("run", help="Full pipeline: generate → start services → run tests → report")
    run_p.add_argument("--feature",       default=None, help="Collection name (default: from config.yaml)")
    run_p.add_argument("--skip-generate", action="store_true",
                       help="Skip generation and reuse existing collection")
    run_p.add_argument("--config",        default="config.yaml")

    # generate
    gen_p = sub.add_parser("generate", help="Generate a Postman collection via LLM")
    gen_p.add_argument("--feature",  default=None,
                       help="Output collection name (default: from config.yaml)")
    gen_p.add_argument("--dry-run",  action="store_true",
                       help="Print prompt and exit without calling LLM")
    gen_p.add_argument("--diff",     action="store_true",
                       help="Only regenerate tests for changed endpoints (incremental)")
    gen_p.add_argument("--base",     default="HEAD",
                       help="Git ref to diff against (default: HEAD)")
    gen_p.add_argument("--config",   default="config.yaml")

    # validate
    val_p = sub.add_parser("validate", help="Validate an existing collection JSON")
    val_p.add_argument("path", help="Path to collection JSON file")
    val_p.add_argument("--config", default="config.yaml")

    # analyze
    ana_p = sub.add_parser("analyze", help="Print analyzed API context (no LLM call)")
    ana_p.add_argument("--diff",   action="store_true",
                       help="Show only changed endpoints")
    ana_p.add_argument("--base",   default="HEAD",
                       help="Git ref to diff against (default: HEAD)")
    ana_p.add_argument("--config", default="config.yaml")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    config = load_config(getattr(args, "config", "config.yaml"))

    if args.command == "run":
        cmd_run(args, config)
    elif args.command == "generate":
        cmd_generate(args, config)
    elif args.command == "validate":
        cmd_validate(args, config)
    elif args.command == "analyze":
        cmd_analyze(args, config)


if __name__ == "__main__":
    main()
