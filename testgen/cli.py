"""
cli.py — command-line entry point for testgen.

Usage:
  python -m testgen generate                        # generate full suite
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
import sys
from pathlib import Path

import yaml

from testgen.analyzer.code_reader      import read_source
from testgen.analyzer.diff_reader      import read_diff_source, build_diff_context
from testgen.analyzer.schema_parser    import parse_schema, schema_to_text
from testgen.analyzer.model_parser     import parse_models, models_to_text
from testgen.generator.prompt_builder  import build_context, SYSTEM_PROMPT
from testgen.generator.llm_client      import LLMClient
from testgen.generator.collection_builder import build_and_save_with_healing, merge_collections
from testgen.validator.validate        import load_collection, validate


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

    if args.command == "generate":
        cmd_generate(args, config)
    elif args.command == "validate":
        cmd_validate(args, config)
    elif args.command == "analyze":
        cmd_analyze(args, config)


if __name__ == "__main__":
    main()
