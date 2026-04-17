"""
validate.py — validates a generated Postman collection file from disk.
Can be called standalone: python -m testgen.validator.validate <path>
"""

import json
import sys
from pathlib import Path


def load_collection(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate(collection: dict) -> dict:
    errors   = []
    warnings = []

    # Top-level structure
    for required in ("info", "item"):
        if required not in collection:
            errors.append(f"Missing top-level field: '{required}'")

    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings, "stats": {}}

    # Schema version
    schema = collection.get("info", {}).get("schema", "")
    if "v2.1.0" not in schema:
        warnings.append(f"Unexpected schema version: {schema}")

    # Walk all items
    total = requests = tested = untested = 0

    def walk(items, path=""):
        nonlocal total, requests, tested, untested
        for item in items:
            total += 1
            name = item.get("name", "<unnamed>")
            loc  = f"{path}/{name}"
            if "item" in item:
                walk(item["item"], loc)
            else:
                requests += 1
                events = item.get("event", [])
                test_events = [e for e in events if e.get("listen") == "test"]
                if not test_events:
                    untested += 1
                    warnings.append(f"No test script: {loc}")
                else:
                    tested += 1
                    exec_lines = test_events[0].get("script", {}).get("exec", [])
                    code = "\n".join(exec_lines)
                    if "pm.test" not in code:
                        warnings.append(f"No pm.test() call in: {loc}")
                    if "pm.response.to.have.status" not in code:
                        warnings.append(f"No status assertion in: {loc}")

    walk(collection["item"])

    stats = {
        "total_items":    total,
        "requests":       requests,
        "tested":         tested,
        "untested":       untested,
    }

    return {
        "valid":    len(errors) == 0,
        "errors":   errors,
        "warnings": warnings,
        "stats":    stats,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m testgen.validator.validate <collection.json>")
        sys.exit(1)

    path = sys.argv[1]
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


if __name__ == "__main__":
    main()
