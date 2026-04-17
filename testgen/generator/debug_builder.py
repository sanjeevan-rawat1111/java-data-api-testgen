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

Tests were run against the live API and some failed. Your job is to fix the
collection so the failing tests pass.

Rules:
1. Output ONLY the complete fixed Postman Collection v2.1.0 JSON — nothing else.
2. Do NOT remove passing tests — only fix the failing ones.
3. Study the response body in each failure to understand the actual API behavior.
4. Common fixes:
   - Wrong expected status code → check the response body for the real status
   - Missing required field in request body → add it based on the model constraints
   - Wrong JSON path in assertion → check the DataResponse structure (success, data, message)
   - Setup not seeding correct data → fix the pre-request SQL INSERT
   - Wrong URL path parameter → check the path template matches the endpoint
5. The response format for ALL endpoints is:
   { "success": true/false, "message": "...", "data": {...}, "source": "...", "executionTimeMs": 0 }
"""


def build_debug_prompt(collection: dict, failures: list[TestFailure]) -> tuple[str, str]:
    """
    Build (system_prompt, user_message) for the LLM debug call.

    Args:
        collection: current Postman collection dict
        failures:   list of TestFailure from the Newman run

    Returns:
        (system_prompt, user_message)
    """
    failure_block = _format_failures(failures)
    collection_json = json.dumps(collection, indent=2)

    user_message = f"""\
=== FAILING TESTS ===
The following {len(failures)} test(s) failed when run against the live API:

{failure_block}

=== CURRENT COLLECTION ===
{collection_json}

Fix the collection so all failing tests pass. Return the complete fixed collection JSON.
"""
    return DEBUG_SYSTEM_PROMPT, user_message


def _format_failures(failures: list[TestFailure]) -> str:
    lines = []
    for i, f in enumerate(failures, 1):
        lines.append(f"Failure {i}:")
        lines.append(f.describe())
        lines.append("")
    return "\n".join(lines)
