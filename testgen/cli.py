"""
cli.py — command-line entry point for testgen.

Usage:
  python -m testgen generate                        # generate full suite
  python -m testgen generate --feature UsersCRUD   # custom output name
  python -m testgen generate --dry-run             # show prompt, skip LLM call
  python -m testgen validate output/MyTests.json   # validate a collection
  python -m testgen analyze                        # print analyzed API context
"""

import argparse
import json
import sys
from pathlib import Path

import yaml

from testgen.analyzer.code_reader    import read_source
from testgen.analyzer.schema_parser  import parse_schema, schema_to_text
from testgen.analyzer.model_parser   import parse_models, models_to_text
from testgen.generator.prompt_builder   import build_context, SYSTEM_PROMPT
from testgen.generator.llm_client       import LLMClient
from testgen.generator.collection_builder import build_and_save_with_healing
from testgen.validator.validate         import load_collection, validate


# ── Config loading ──────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        print(f"[warn] config.yaml not found at {path}, using defaults")
        return {}
    with open(config_path) as f:
        return yaml.safe_load(f) or {}


# ── Subcommands ─────────────────────────────────────────────────────────────

def cmd_generate(args, config: dict):
    repo_root  = config.get("java_data_api_path", "../java-data-api")
    output_dir = config.get("output_dir", "collections")
    feature    = args.feature or config.get("default_feature_name", "java-data-api-tests")

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

    max_retries = config.get("max_retries", 3)
    print(f"[3/3] Calling LLM ({config.get('provider','openai')} / {config.get('model','gpt-4o')}) "
          f"— self-healing enabled (max {max_retries} attempts)...")
    result = build_and_save_with_healing(
        llm_client=LLMClient(config),
        system_prompt=SYSTEM_PROMPT,
        user_message=user_message,
        feature_name=feature,
        output_dir=output_dir,
    )

    healed = " (self-healed)" if result.get("self_healed") else ""
    print(f"\n✔ Collection saved to: {result['output_path']}{healed}")
    print(f"  Requests generated : {result['total_requests']}")
    print(f"  LLM attempts used  : {result['attempts']}/{config.get('max_retries', 3)}")
    print(f"\n→  Run tests:  docker-compose --profile run up newman-runner")
    if result["warnings"]:
        print(f"  Warnings ({len(result['warnings'])}):")
        for w in result["warnings"]:
            print(f"    ⚠ {w}")


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
    repo_root   = config.get("java_data_api_path", "../java-data-api")
    source_data = read_source(repo_root)

    from testgen.analyzer.endpoint_parser import parse_endpoints
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


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="testgen",
        description="AI-powered Postman test generator for java-data-api",
    )
    sub = parser.add_subparsers(dest="command")

    # generate
    gen_p = sub.add_parser("generate", help="Generate a Postman collection via LLM")
    gen_p.add_argument("--feature", default=None,
                       help="Output collection name (default: from config.yaml)")
    gen_p.add_argument("--dry-run", action="store_true",
                       help="Print prompt and exit without calling LLM")
    gen_p.add_argument("--config", default="config.yaml",
                       help="Path to config.yaml")

    # validate
    val_p = sub.add_parser("validate", help="Validate an existing collection JSON")
    val_p.add_argument("path", help="Path to collection JSON file")
    val_p.add_argument("--config", default="config.yaml")

    # analyze
    ana_p = sub.add_parser("analyze", help="Print analyzed API context (no LLM call)")
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
