"""
collection_builder.py — parses LLM output into a validated Postman collection
and writes it to the output directory.

Self-healing integration:
  build_and_save_with_healing() uses LLMClient.generate_with_healing() so that
  structurally invalid output (missing 'item', no test scripts, bad JSON) is
  automatically sent back to the LLM for correction before giving up.
"""

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Parse ────────────────────────────────────────────────────────────────────

def parse_llm_output(raw: str) -> dict:
    """
    Extract JSON from LLM response.
    Handles cases where the model wraps JSON in markdown fences.
    """
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM output is not valid JSON: {e}\n\nRaw output:\n{raw[:500]}")


# ── Validate ─────────────────────────────────────────────────────────────────

def validate_collection(collection: dict) -> list[str]:
    """
    Structural validation of a Postman Collection v2.1.0.
    Returns a list of error/warning strings (empty = valid).
    """
    warnings = []

    if "info" not in collection:
        warnings.append("Missing 'info' field")
    if "item" not in collection:
        warnings.append("Missing 'item' field — no test folders or requests")
        return warnings

    def check_items(items: list, path: str = "root"):
        for i, item in enumerate(items):
            loc = f"{path}[{i}] '{item.get('name', '?')}'"
            if "item" in item:
                check_items(item["item"], loc)
            elif "request" not in item:
                warnings.append(f"{loc}: leaf item has no 'request'")
            else:
                if "event" not in item:
                    warnings.append(f"{loc}: no test script (missing 'event')")

    check_items(collection["item"])
    return warnings


def structural_validator_for_healing(raw: str) -> list[str]:
    """
    Used as validate_fn in generate_with_healing().
    Parses raw text then runs structural validation.
    Returns error strings; empty list = OK to accept.
    """
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    try:
        col = json.loads(cleaned)
    except json.JSONDecodeError as e:
        return [f"JSON parse error: {e}"]
    errors = validate_collection(col)
    # Only block on critical errors (missing top-level structure)
    critical = [e for e in errors if "Missing 'item'" in e or "Missing 'info'" in e]
    return critical


# ── Enrich + Write ────────────────────────────────────────────────────────────

def enrich_collection(collection: dict, feature_name: str) -> dict:
    collection.setdefault("info", {})
    collection["info"]["name"]   = feature_name
    collection["info"]["schema"] = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
    collection["info"].setdefault("_postman_id", str(uuid.uuid4()))
    return collection


def write_collection(collection: dict, output_dir: str, feature_name: str) -> str:
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\-]", "_", feature_name)
    out_path  = Path(output_dir) / f"{safe_name}.json"
    out_path.write_text(json.dumps(collection, indent=2), encoding="utf-8")
    return str(out_path)


def _count_requests(items: list) -> int:
    count = 0
    for item in items:
        if "item" in item:
            count += _count_requests(item["item"])
        elif "request" in item:
            count += 1
    return count


# ── Simple build (no healing) ─────────────────────────────────────────────────

def build_and_save(raw_llm_output: str, feature_name: str, output_dir: str) -> dict:
    """Parse → validate → enrich → write (no retry)."""
    collection = parse_llm_output(raw_llm_output)
    warnings   = validate_collection(collection)
    collection = enrich_collection(collection, feature_name)
    out_path   = write_collection(collection, output_dir, feature_name)
    return {
        "output_path":    out_path,
        "warnings":       warnings,
        "total_requests": _count_requests(collection.get("item", [])),
        "feature_name":   feature_name,
        "generated_at":   datetime.utcnow().isoformat() + "Z",
        "attempts":       1,
    }


# ── Self-healing build ────────────────────────────────────────────────────────

def build_and_save_with_healing(
    llm_client,
    system_prompt: str,
    user_message: str,
    feature_name: str,
    output_dir: str,
) -> dict:
    """
    Full self-healing pipeline:
      1. Call LLM
      2. If output is bad JSON or missing critical structure → send error
         back to LLM and ask it to fix (up to max_retries times)
      3. Parse final output → validate → enrich → write

    The healing loop lives inside LLMClient.generate_with_healing().
    This function orchestrates the result.
    """
    logger.info("Starting self-healing generation for '%s'", feature_name)

    raw, attempts = llm_client.generate_with_healing(
        system_prompt=system_prompt,
        user_message=user_message,
        validate_fn=structural_validator_for_healing,
    )

    if attempts > 1:
        logger.info("Self-healing used %d attempt(s) to produce valid output", attempts)

    try:
        collection = parse_llm_output(raw)
    except ValueError as e:
        raise ValueError(
            f"Output still invalid after {attempts} attempt(s): {e}"
        ) from e

    warnings   = validate_collection(collection)
    collection = enrich_collection(collection, feature_name)
    out_path   = write_collection(collection, output_dir, feature_name)

    return {
        "output_path":    out_path,
        "warnings":       warnings,
        "total_requests": _count_requests(collection.get("item", [])),
        "feature_name":   feature_name,
        "generated_at":   datetime.utcnow().isoformat() + "Z",
        "attempts":       attempts,
        "self_healed":    attempts > 1,
    }


# ── Incremental merge ─────────────────────────────────────────────────────────

def merge_collections(base_path: str, patch_path: str) -> dict:
    """
    Merge a patch collection into a base collection.

    Strategy:
    - For each folder in the patch, find the matching folder in base by name.
      - If found: replace requests inside that folder that share the same name,
        and append any brand-new requests.
      - If not found: append the entire patch folder to base.
    - Top-level requests (not in folders) are merged the same way.
    """
    with open(base_path)  as f: base  = json.load(f)
    with open(patch_path) as f: patch = json.load(f)

    base_items  = base.get("item", [])
    patch_items = patch.get("item", [])

    def item_name(item):
        return item.get("name", "")

    def is_folder(item):
        return "item" in item

    def merge_folder_items(base_folder_items: list, patch_folder_items: list) -> list:
        """Merge two flat lists of request items by name."""
        base_by_name = {item_name(i): i for i in base_folder_items}
        result = list(base_folder_items)
        for patch_item in patch_folder_items:
            name = item_name(patch_item)
            if name in base_by_name:
                # Replace the matching item
                idx = next(i for i, x in enumerate(result) if item_name(x) == name)
                result[idx] = patch_item
            else:
                result.append(patch_item)
        return result

    # Build lookup of base folders by name
    base_folders_by_name = {item_name(i): i for i in base_items if is_folder(i)}
    result_items = list(base_items)

    for patch_item in patch_items:
        name = item_name(patch_item)
        if is_folder(patch_item):
            if name in base_folders_by_name:
                # Merge requests inside matching folder
                base_folder = base_folders_by_name[name]
                base_folder["item"] = merge_folder_items(
                    base_folder.get("item", []),
                    patch_item.get("item", []),
                )
            else:
                result_items.append(patch_item)
        else:
            # Top-level request — replace by name or append
            names = [item_name(i) for i in result_items]
            if name in names:
                idx = names.index(name)
                result_items[idx] = patch_item
            else:
                result_items.append(patch_item)

    base["item"] = result_items
    return base
