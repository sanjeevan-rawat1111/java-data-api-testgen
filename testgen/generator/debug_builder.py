"""
debug_builder.py — builds the LLM prompt for the self-debug loop.

When tests fail after generation, the debug loop sends the failing collection
back to the LLM together with structured failure details so it can fix the
specific tests that failed without regenerating the whole collection.
"""

import json
from testgen.runner.newman_runner import TestFailure


DEBUG_SYSTEM_PROMPT = """\
You are an expert software test engineer debugging a Postman test collection.

Tests were run against a live API and some requests failed. Your job is to fix
ONLY the failing request items and return them as a JSON array.

Rules:
1. Output ONLY a JSON array of fixed request item objects — nothing else.
   e.g.  [{"name":"Get User By Email","request":{...},"event":[...]}]
2. Return ONLY the items listed below — do NOT add other items.
3. Preserve the exact "name" field — the caller matches by name to merge.
4. Study the failure message to understand what went wrong.
5. Common fixes:
   - Unexpected identifier / SyntaxError → fix SQL VALUES quoting (use ' not " for SQL strings)
   - data: null / data not found → the pre-request INSERT failed; fix SQL in the prereq body
   - data exists: null → prereq seed not working; verify the /query body is valid JSON
6. Response format for ALL endpoints:
   { "success": true/false, "message": "...", "data": {...}, "source": "...", "executionTimeMs": 0 }

!! CRITICAL SQL QUOTING — SQL VALUES must use SINGLE QUOTES, never double quotes !!
CORRECT:  "INSERT INTO users (...) VALUES ('Test User','test@x.com',30,'NYC')"
BROKEN:   "INSERT INTO users (...) VALUES ('Test User\","test@x.com\",30,'NYC')"

!! HELPER ENDPOINT CONTRACTS — READ CAREFULLY !!
POST /query         → runs SQL, returns {"1":true,"2":true} — BOOLEANS ONLY, NO ROW DATA
POST /select_query  → runs SELECT, returns {"1":[{"id":5,...}]} — ACTUAL ROWS

!! FOR PUT/DELETE FAILURES (400, {{testUserId}} unresolved) — DO NOT add prerequest scripts !!
The collection already has separate "Setup" items before Update/Delete that seed the user
and capture the id. If testUserId is still not set, the issue is in those Setup items —
FIX THE SETUP ITEMS (Setup — Seed User, Setup — Get User Id), not the PUT/DELETE item itself.
DO NOT add a pm.sendRequest prerequest to the PUT or DELETE item.

!! pm.sendRequest IN PREREQUEST SCRIPTS IS ASYNC — NEVER USE IT TO CAPTURE IDs !!
Newman fires the main request before the callback runs, so the variable is never set.
The correct approach (already in the collection's Setup items):
  Setup — Seed User:     POST /query   body: {"1":"DELETE...","2":"INSERT..."}
  Setup — Get User Id:   POST /select_query   body: {"1":"SELECT id FROM users WHERE email='...'"}
                         test script: var rows=pm.response.json()["1"]; pm.environment.set("testUserId",rows[0].id);
  Update/Delete item: no prerequest, URL uses {{testUserId}}

!! NEVER HARDCODE HOSTNAMES !!
- API URLs: "{{BASE_URL}}/data/..."
- Helper in scripts: pm.environment.get("HELPER_HOST") + "/..."
"""


_MAX_FAILURES = 8   # keep token count well under Groq 12 K TPM limit


def build_debug_prompt(collection: dict, failures: list[TestFailure]) -> tuple[str, str]:
    """
    Build (system_prompt, user_message) for the LLM debug call.

    Sends ONLY the failing request items (not the full collection) to stay under
    the Groq free-tier 12 K TPM limit.  The caller merges the returned fixed
    items back into the original collection by matching on item name.

    Args:
        collection: current Postman collection dict
        failures:   list of TestFailure from the Newman run

    Returns:
        (system_prompt, user_message)
    """
    unique = _dedupe_failures(failures)[:_MAX_FAILURES]
    failure_block = _format_failures(unique)

    failing_names = {f.request for f in unique if f.request and f.request != "unknown"}
    failing_items = _extract_failing_items(collection.get("item", []), failing_names)
    items_json    = json.dumps(failing_items, separators=(",", ":"))

    truncation_note = ""
    if len(unique) < len(failures):
        truncation_note = (
            f"\n(Showing {len(unique)} of {len(failures)} failures — "
            "fix these patterns and the rest will follow.)\n"
        )

    user_message = f"""\
=== FAILING TESTS ==={truncation_note}
{len(unique)} test(s) failed when run against the live API:

{failure_block}

=== FAILING REQUEST ITEMS (fix these and return as a JSON array) ===
{items_json}

Return a JSON ARRAY of the fixed request items — no wrapper, no full collection.
"""
    return DEBUG_SYSTEM_PROMPT, user_message


def _extract_failing_items(items: list, failing_names: set) -> list:
    """
    Recursively walk the collection item tree and return only the leaf request
    items whose name is in failing_names.
    """
    result = []
    for item in items:
        if "item" in item:
            sub = _extract_failing_items(item["item"], failing_names)
            result.extend(sub)
        elif item.get("name") in failing_names:
            result.append(item)
    return result


def _dedupe_failures(failures: list[TestFailure]) -> list[TestFailure]:
    """Keep at most one failure per (request, test) pair to reduce duplicates."""
    seen: set[tuple[str, str]] = set()
    result = []
    for f in failures:
        key = (f.request, f.test)
        if key not in seen:
            seen.add(key)
            result.append(f)
    return result


def _format_failures(failures: list[TestFailure]) -> str:
    lines = []
    for i, f in enumerate(failures, 1):
        lines.append(f"Failure {i}:")
        lines.append(f.describe())
        lines.append("")
    return "\n".join(lines)
