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
# re is used both in the module body and in _fix_sql_quoting
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


# ── URL normalizer ────────────────────────────────────────────────────────────

def _normalize_url(url) -> str:
    """
    Normalize a request URL to a plain string.

    When the LLM generates URL objects like:
      {"raw": "{{BASE_URL}}/data/users", "host": ["{{BASE_URL}}"], "path": [...]}
    Newman uses the `host` array for DNS resolution.  If the env variable
    value includes the protocol (e.g. `http://localhost:8081/api`), the full
    value ends up as the hostname, causing `getaddrinfo ENOTFOUND {{BASE_URL}}`.

    Returning `url.raw` as a plain string makes Newman parse the substituted
    value as a complete URL (protocol + host + port + path), which works correctly
    regardless of whether the variable contains a protocol.
    """
    if isinstance(url, str):
        return url
    if isinstance(url, dict) and url.get("raw"):
        return url["raw"]
    return url


def normalize_collection_urls(collection: dict) -> dict:
    """Walk every request item and flatten URL objects to plain strings."""

    def fix_items(items: list):
        for item in items:
            if "item" in item:
                fix_items(item["item"])
            elif "request" in item:
                req = item["request"]
                if "url" in req:
                    req["url"] = _normalize_url(req["url"])

    fix_items(collection.get("item", []))
    return collection


# ── Enrich + Write ────────────────────────────────────────────────────────────

_COLLECTION_PREREQ_EXEC = [
    "// One-time DB + Aerospike setup (runs before every request, no-ops after first run)",
    "if (!pm.collectionVariables.get('_setupDone')) {",
    "  var _h = pm.environment.get('HELPER_HOST');",
    "  pm.sendRequest({",
    "    url: _h + '/setdb', method: 'POST',",
    "    header: [{ key: 'Content-Type', value: 'application/json' }],",
    "    body: { mode: 'raw', raw: JSON.stringify({",
    "      host: pm.environment.get('DB_HOSTNAME'), port: pm.environment.get('DB_PORT'),",
    "      user: pm.environment.get('DB_USER'), password: pm.environment.get('DB_PASS'),",
    "      database: pm.environment.get('DB_NAME')",
    "    })}",
    "  }, function(err, res) { console.log('DB setup:', res ? res.code : String(err)); });",
    "  pm.sendRequest({",
    "    url: _h + '/aerospike/connect', method: 'POST',",
    "    header: [{ key: 'Content-Type', value: 'application/json' }],",
    "    body: { mode: 'raw', raw: JSON.stringify({",
    "      host: pm.environment.get('AERO_HOST'), port: pm.environment.get('AERO_PORT')",
    "    })}",
    "  }, function(err, res) { console.log('Aerospike setup:', res ? res.code : String(err)); });",
    "  pm.collectionVariables.set('_setupDone', true);",
    "}",
]

_SQL_OPEN_RE  = re.compile(r"'(DELETE |INSERT |UPDATE |SELECT |TRUNCATE )")
# After replacing the opening ' with ", the trailing outer ' remains before a separator.
# Pattern: the SQL value (now double-quoted) ends with an extra ' before , or whitespace/brace.
_SQL_CLOSE_RE = re.compile(
    r'("(?:DELETE|INSERT|UPDATE|SELECT|TRUNCATE)\s[^"]*)'   # SQL value content — greedy, no " inside
    r"'"                                                    # the extra trailing outer '
    r"([,\s\}])"                                           # separator that follows it
)

# Matches: var body = '...' where the outer quotes are single-quoted JS string.
# The greedy .* captures up to the LAST ' before the ; so even strings containing
# SQL single-quote values are handled correctly.
_VAR_BODY_SINGLE_RE = re.compile(
    r"^(var\s+(?:body|cleanup|q|sql)\s*=\s*)'(\{.*)'(;.*)$",
    re.DOTALL,
)

# Matches: var body = `...` (backtick template literal)
_VAR_BODY_BACKTICK_RE = re.compile(
    r"^(var\s+(?:body|cleanup|q|sql)\s*=\s*)`(.*)`(;.*)$",
    re.DOTALL,
)


def _fix_backtick_sql_values(line: str) -> str:
    """
    Fix SQL VALUES clauses inside backtick template literals where the LLM
    used " as SQL string delimiter instead of '.

    The LLM generates (broken JSON inside backtick):
      var body = `{"2":"INSERT INTO users (...) VALUES ('Test User","test@x.com",30,'NYC')"}`;

    After fix:
      var body = `{"2":"INSERT INTO users (...) VALUES ('Test User','test@x.com',30,'NYC')"}`;

    The fixer extracts the backtick content, tries json.loads, and if that fails
    applies targeted substitutions to restore single-quote SQL delimiters.
    """
    import json as _json

    m = _VAR_BODY_BACKTICK_RE.match(line)
    if not m:
        return line
    prefix, content, suffix = m.group(1), m.group(2), m.group(3)

    # If the JSON is already valid, nothing to do
    try:
        _json.loads(content)
        return line
    except (ValueError, _json.JSONDecodeError):
        pass

    # Pass 1: SQL string value closed with " instead of ' — 'value", (spaces allowed)
    content = re.sub(r"'([^'\"(),]+)\"([,)])", r"'\1'\2", content)
    # Pass 2: SQL string value opened AND closed with " — ,"value", or ("value",
    content = re.sub(r'([,(])"([^"\'(),]+)"([,)])', r"\1'\2'\3", content)
    # Pass 3: trailing "value" before ) at end of VALUES list
    content = re.sub(r'([,(])"([^"\'(),]+)"(?=[)}])', r"\1'\2'", content)

    return prefix + "`" + content + "`" + suffix


def _fix_sql_quoting(exec_lines: list[str]) -> list[str]:
    """
    Fix SQL strings with embedded single quotes that cause JS SyntaxErrors.

    Class A — whole body wrapped in single-quoted JS string:
      var body = '{"1":"DELETE ... WHERE email = 'x@y.com'"}';
      → backtick literal:
      var body = `{"1":"DELETE ... WHERE email = 'x@y.com'"}`;

    Class B — backtick literal with broken VALUES quoting ("  instead of '):
      var body = `{"2":"INSERT ... VALUES ('Test User\","test@x.com\")"}`;
      → fixed VALUES quoting:
      var body = `{"2":"INSERT ... VALUES ('Test User','test@x.com')"}`;

    Class C — legacy JSON.stringify with single-quoted SQL values.
    """
    fixed = []
    for line in exec_lines:
        # Class A: single-quoted var body/cleanup → backtick
        m = _VAR_BODY_SINGLE_RE.match(line)
        if m:
            line = m.group(1) + "`" + m.group(2) + "`" + m.group(3)
            # fall through to Class B check on the now-backtick line

        # Class B: backtick body with broken SQL VALUES quoting
        line = _fix_backtick_sql_values(line)

        # Class C (legacy): digit keys + SQL opening quotes → double quotes
        if "`" not in line:
            line = re.sub(r"'(\d+)':", r'"\1":', line)
            line = _SQL_OPEN_RE.sub(lambda m: '"' + m.group(1), line)
            prev = None
            while prev != line:
                prev = line
                line = _SQL_CLOSE_RE.sub(r'\1"\2', line)

        # Class D: fix SQL VALUES quoting on any exec line that contains a VALUES clause.
        # Targets mixed-quote patterns the LLM generates inside JSON.stringify({...}) bodies:
        #   'Test User","test@example.com"  →  'Test User','test@example.com'
        # The guard ensures only SQL-context lines are touched (not pm.test/pm.expect lines).
        if "VALUES" in line.upper():
            # Pass 1: SQL string closed with " instead of ' — 'value",  or  'value")
            line = re.sub(r"'([^'\"(),]+)\"([,)])", r"'\1'\2", line)
            # Pass 2: SQL string surrounded by " — ,"value",  or  ("value",
            line = re.sub(r'([,(])"([^"\'(),]+)"([,)])', r"\1'\2'\3", line)
            # Pass 3: SQL string at end of VALUES list — ,"value")
            line = re.sub(r'([,(])"([^"\'(),]+)"(?=[)}])', r"\1'\2'", line)

        fixed.append(line)
    return fixed


_HARDCODED_ID_RE = re.compile(
    r"(\{\{BASE_URL\}\}/(?:data/)?[a-z]+/)\d+$",
    re.IGNORECASE,
)


def _fix_hardcoded_ids(collection: dict) -> dict:
    """
    Replace hardcoded numeric IDs in PUT/DELETE request URLs with {{testUserId}}.

    LLM pattern (broken — fails on fresh DB where id ≠ 1):
      PUT  {{BASE_URL}}/data/users/1
      DELETE {{BASE_URL}}/data/users/1

    After fix:
      PUT  {{BASE_URL}}/data/users/{{testUserId}}
      DELETE {{BASE_URL}}/data/users/{{testUserId}}
    """
    def fix_items(items: list):
        for item in items:
            if "item" in item:
                fix_items(item["item"])
                continue
            req = item.get("request", {})
            method = req.get("method", "").upper()
            if method not in ("PUT", "DELETE", "PATCH"):
                continue
            url = req.get("url", "")
            if isinstance(url, str) and _HARDCODED_ID_RE.search(url):
                req["url"] = _HARDCODED_ID_RE.sub(r"\1{{testUserId}}", url)

    fix_items(collection.get("item", []))
    return collection


def _add_collection_prereq(collection: dict) -> dict:
    """
    Replace any LLM-generated DB Setup / Aerospike Setup request folders with a
    canonical collection-level pre-request event so Newman never has to resolve
    {{HELPER_HOST}} in a top-level request URL (which Newman mishandles when the
    value contains the http:// scheme).
    """
    # Remove the setup folders if present
    setup_names = {"DB Setup", "Aerospike Setup", "db setup", "aerospike setup"}
    collection["item"] = [
        f for f in collection.get("item", [])
        if f.get("name", "") not in setup_names
    ]

    # Inject collection-level prerequest (skip if already present)
    events = collection.setdefault("event", [])
    if not any(e.get("listen") == "prerequest" for e in events):
        events.insert(0, {
            "listen": "prerequest",
            "script": {
                "type": "text/javascript",
                "exec": _COLLECTION_PREREQ_EXEC,
            },
        })
    return collection


# Matches any object property line (quoted "key": or unquoted key:) that ends with a
# value character (not a comma or an opening bracket/paren).
# Used to detect missing commas between consecutive property lines.
_PROP_LINE_RE = re.compile(
    r'^\s+(?:"[^"]+"|[\w$]+)\s*:.*[^,{(\[]\s*$'
)

# Keep the old narrower name as an alias so existing call-sites still work.
_JSON_PROP_RE = _PROP_LINE_RE

# Matches {{VAR_NAME}} used as a quoted JSON value inside a JS exec line.
# Newman does NOT substitute {{vars}} in exec scripts — must use pm.environment.get().
_ENV_VAR_IN_SCRIPT_RE = re.compile(r'"(\{\{([A-Z_][A-Z0-9_]*)\}\})"')


def _fix_missing_commas(exec_lines: list[str]) -> list[str]:
    """
    Fix missing commas between consecutive object property lines.

    Handles both JSON.stringify numeric properties AND JS object literal properties:
      header: [...]  followed by  body: {...}     → comma after header line
      "2": "INSERT..."  followed by  "3": "SELECT..."  → comma after "2" line

    A property line is any indented line of the form  key: value  (quoted or unquoted key)
    whose last non-whitespace character is NOT one of  , { ( [
    """
    fixed = list(exec_lines)
    for i in range(len(fixed) - 1):
        cur = fixed[i]
        nxt = fixed[i + 1]
        if (not cur.rstrip().endswith(",")
                and _PROP_LINE_RE.match(cur)
                and re.match(r'^\s+(?:"[^"]+"|[\w$]+)\s*:', nxt)):
            fixed[i] = cur.rstrip() + ","
    return fixed


def _fix_double_escaped_pm_test(exec_lines: list[str]) -> list[str]:
    """
    Fix pm.test() calls where the LLM double-escaped quotes and split the name string
    across two exec-array elements.

    Case 1 — original two-element form (pm.test(\ on its own line):
      'pm.test(\\"status is 200\\'   ← literal backslash-quote prefix, split at callback
      '() => pm.response.to.have.status(200));'
      → 'pm.test("status is 200", () => pm.response.to.have.status(200));'

    Case 2 — already merged but backslash-space separator remains:
      'pm.test("status is 200\\ () => pm.response.to.have.status(200));'
      → 'pm.test("status is 200", () => pm.response.to.have.status(200));'
    """
    result = []
    i = 0
    while i < len(exec_lines):
        line = exec_lines[i]
        # Case 1: original two-element form — pm.test(\ prefix
        if re.match(r'pm\.test\(\\', line) and i + 1 < len(exec_lines):
            nxt = exec_lines[i + 1]
            # Unescape \" → " and \' → '
            clean = re.sub(r'\\(["\'])', r'\1', line)
            # Strip ONLY trailing backslashes (preserve the closing quote of the name arg)
            # then add '",' to properly close the string and separate from the callback
            stripped = re.sub(r'\\+$', '', clean.rstrip()) + '",'
            result.append(stripped + " " + nxt.strip())
            i += 2
        else:
            # Case 2: merged form with backslash-space separator  pm.test("text\ ()
            fixed = re.sub(r'(pm\.test\()(".*?)\\ ', r'\1\2", ', line)
            # Case 3: merged form missing closing quote  pm.test("text, ()
            fixed = re.sub(r'(pm\.test\(")([^"]+),(\s*\(\))', r'\1\2", \3', fixed)
            result.append(fixed)
            i += 1
    return result


def _fix_env_vars_in_scripts(exec_lines: list[str]) -> list[str]:
    """
    Replace "{{VAR}}" (Postman variable as a quoted string) in exec lines with
    pm.environment.get("VAR").  Newman does not resolve {{...}} inside JS scripts.

    e.g.  "namespace": "{{AERO_NAMESPACE}}"
       →  "namespace": pm.environment.get("AERO_NAMESPACE")
    """
    fixed = []
    for line in exec_lines:
        line = _ENV_VAR_IN_SCRIPT_RE.sub(
            lambda m: f'pm.environment.get("{m.group(2)}")',
            line,
        )
        fixed.append(line)
    return fixed


def _fix_all_scripts(collection: dict) -> dict:
    """Walk every exec script in the collection and fix SQL quoting and env var usage."""
    def fix_exec(exec_lines: list) -> list:
        exec_lines = _fix_sql_quoting(exec_lines)
        exec_lines = _fix_double_escaped_pm_test(exec_lines)
        exec_lines = _fix_missing_commas(exec_lines)
        exec_lines = _fix_env_vars_in_scripts(exec_lines)
        return exec_lines

    def fix_items(items):
        for item in items:
            if "item" in item:
                fix_items(item["item"])
            for evt in item.get("event", []):
                s = evt.get("script", {})
                if "exec" in s:
                    s["exec"] = fix_exec(s["exec"])

    fix_items(collection.get("item", []))
    # Also fix collection-level events
    for evt in collection.get("event", []):
        s = evt.get("script", {})
        if "exec" in s:
            s["exec"] = fix_exec(s["exec"])
    return collection


def enrich_collection(collection: dict, feature_name: str) -> dict:
    collection.setdefault("info", {})
    collection["info"]["name"]   = feature_name
    collection["info"]["schema"] = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
    collection["info"].setdefault("_postman_id", str(uuid.uuid4()))
    normalize_collection_urls(collection)
    _fix_hardcoded_ids(collection)
    _add_collection_prereq(collection)
    _fix_all_scripts(collection)
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
    analysis_client=None,
) -> dict:
    """
    Full self-healing pipeline:
      1. Call generation LLM (llm_client) for the initial attempt
      2. If output is bad JSON or missing critical structure → send error
         back to the analysis LLM (analysis_client, e.g. Claude Sonnet) and
         ask it to fix (up to max_retries times)
      3. Parse final output → validate → enrich → write

    Dual-model pattern:
      llm_client      — generation model (e.g. Claude Opus): produces the collection
      analysis_client — analysis model (e.g. Claude Sonnet): handles self-healing
                        correction retries; falls back to llm_client if None

    The healing loop lives inside LLMClient.generate_with_healing().
    This function orchestrates the result.
    """
    logger.info("Starting self-healing generation for '%s'", feature_name)

    raw, attempts = llm_client.generate_with_healing(
        system_prompt=system_prompt,
        user_message=user_message,
        validate_fn=structural_validator_for_healing,
        analysis_client=analysis_client,
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
