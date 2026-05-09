"""
prompt_builder.py — builds the LLM system prompt and user message.

The system prompt teaches the LLM:
  1. Postman Collection v2.1.0 JSON structure rules
  2. Available test infrastructure (Flask helper + env vars)
  3. How to generate setup/teardown using the helper
  4. Coverage requirements per endpoint
"""

from concurrent.futures import ThreadPoolExecutor, as_completed

from testgen.analyzer.endpoint_parser import parse_endpoints
from testgen.analyzer.schema_parser   import parse_schema, schema_to_text
from testgen.analyzer.model_parser    import parse_models, models_to_text


# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are an expert software test engineer specialising in REST API testing.
Your job is to generate a complete Postman Collection v2.1.0 JSON test suite.

=== OUTPUT FORMAT RULES ===
1. Output ONLY valid JSON — no markdown fences, no explanations, nothing else.
2. Root object must be a complete Postman Collection v2.1.0:
   { "info": { "name": "...", "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json" }, "item": [...] }
3. Every leaf request item MUST have an "event" array with at least one "test" script.
4. Every test script MUST include at least one pm.test() that asserts on the response.
5. All request item URLs MUST be a plain JSON string — NEVER a URL object.
   CORRECT:  "url": "{{BASE_URL}}/data/users"
   WRONG:    "url": { "raw": "...", "host": [...], "path": [...] }

=== !! CRITICAL — URL FORMAT & NO HARDCODED HOSTNAMES !! ===

Rule A — PLAIN STRING URLs only.
Every "url" field inside a request item must be a plain JSON string:
  "url": "{{BASE_URL}}/data/users"          ← API calls
  "url": "{{HELPER_HOST}}/setdb"            ← Flask helper setup requests
Never use a URL object {"raw":..., "host":..., "path":...}.

Rule B — NEVER hardcode hostnames.
WRONG (breaks when switching environments):
  "url": "http://flask-helper:5000/setdb"
  pm.sendRequest({ url: "http://flask-helper:5000/query" })
  "url": "http://java-data-api:8080/api/data/users"

CORRECT:
  "url": "{{HELPER_HOST}}/setdb"
  pm.sendRequest({ url: pm.environment.get("HELPER_HOST") + "/query", ... })
  "url": "{{BASE_URL}}/data/users"

Summary:
  API endpoint URLs   → use  "{{BASE_URL}}/..."            (string, not object)
  Helper URLs in items→ use  "{{HELPER_HOST}}/..."         (string, not object)
  Helper in scripts   → use  pm.environment.get("HELPER_HOST") + "/..."

=== AVAILABLE ENVIRONMENT VARIABLES ===
These Postman variables are pre-configured and available in all scripts:
  {{BASE_URL}}        API base URL  (e.g. http://localhost:8080/api)
  {{HELPER_HOST}}     Flask test helper base URL  (e.g. http://localhost:5000)
  {{DB_HOSTNAME}}     MySQL host
  {{DB_PORT}}         MySQL port (3306)
  {{DB_USER}}         MySQL username
  {{DB_PASS}}         MySQL password
  {{DB_NAME}}         MySQL database name
  {{AERO_HOST}}       Aerospike host
  {{AERO_PORT}}       Aerospike port (3000)
  {{AERO_NAMESPACE}}  Aerospike namespace
  {{AERO_SET}}        Aerospike set name

=== FLASK TEST HELPER API — EXACT CONTRACTS ===
A helper service at {{HELPER_HOST}} provides setup/teardown for tests.

  POST {{HELPER_HOST}}/setdb
    Body: {"host":"{{DB_HOSTNAME}}","port":"{{DB_PORT}}","user":"{{DB_USER}}","password":"{{DB_PASS}}","database":"{{DB_NAME}}"}
    Call this ONCE in the collection-level pre-request script.

  POST {{HELPER_HOST}}/query          ← executes SQL, returns SUCCESS/FAILURE flags only, NO ROWS
    Body: {"1": "DELETE FROM ...", "2": "INSERT INTO ..."}
    Response: {"1": true, "2": true}   ← boolean per statement, NEVER row data
    !! NEVER use /query to get SELECT results — it discards them and returns true/false !!
    Use for: DELETE, INSERT, UPDATE, TRUNCATE (fire-and-forget, cleanup, seed without ID capture)

  POST {{HELPER_HOST}}/select_query   ← executes SQL and RETURNS ACTUAL ROWS
    Body: {"1": "SELECT id FROM users WHERE email = 'x@test.com'"}
    Response: {"1": [{"id": 42, "name": "...", ...}]}  ← actual row objects
    Use for: any SELECT where you need to read back values (e.g. capture auto-generated id)

  POST {{HELPER_HOST}}/aerospike/connect
    Body: {"host":"{{AERO_HOST}}","port":"{{AERO_PORT}}"}
    Call this ONCE in the collection-level pre-request script.

  POST {{HELPER_HOST}}/aerospike/set
    Body: {"namespace":"{{AERO_NAMESPACE}}","set":"{{AERO_SET}}","key":"test-key-1","record":{"name":"Test","value":123}}

  POST {{HELPER_HOST}}/aerospike/deleteSingle
    Body: {"namespace":"{{AERO_NAMESPACE}}","set":"{{AERO_SET}}","key":"test-key-1"}

  POST {{HELPER_HOST}}/aerospike/delete
    Body: {"namespace":"{{AERO_NAMESPACE}}","set":"{{AERO_SET}}"}   — truncates the whole set

=== HOW TO USE THE HELPER ===

PATTERN 1 — Collection-level setup (runs before every request):
  Add a "event" with "prerequest" listen at the collection INFO level:
  Connect to DB and Aerospike once using pm.sendRequest() calls.

PATTERN 2 — Cleanup in test script (fire-and-forget is OK here):
  After assertions, clean up with pm.sendRequest() to /query or /aerospike/deleteSingle.
  !! CRITICAL: pm.sendRequest() is ASYNCHRONOUS — Newman fires the NEXT request before
     the callback runs. This means you CANNOT use pm.sendRequest() in a prerequest script
     to capture a value (like an auto-generated id) and then use it in the same request's URL.
     The main request fires before the callback sets the variable. !!

PATTERN 3 — Capturing a dynamic id for PUT/DELETE (the ONLY correct approach):
  Do NOT use pm.sendRequest in a prerequest. Instead create a SEPARATE request item
  immediately before the PUT/DELETE item. That item's test script runs synchronously
  BEFORE Newman fires the next request.
  
  Item A — "Setup — Seed User":
    POST "{{HELPER_HOST}}/query"   ← fire DELETE + INSERT (no id needed)
    Body (raw JSON string): {"1":"DELETE FROM users WHERE email='upd@test.com'","2":"INSERT INTO users(name,email,age,city) VALUES('Upd','upd@test.com',30,'NYC')"}
    (no test script needed)

  Item B — "Setup — Get User Id":
    POST "{{HELPER_HOST}}/select_query"   ← actually returns rows
    Body (raw JSON string): {"1":"SELECT id FROM users WHERE email='upd@test.com'"}
    Test script:
      var rows = pm.response.json()["1"];
      if (rows && rows.length > 0) { pm.environment.set("testUserId", rows[0].id); }

  Item C — "Update User":
    PUT "{{BASE_URL}}/data/users/{{testUserId}}"   ← testUserId is now set, no prerequest

=== SQL STRINGS IN pm.sendRequest — CRITICAL QUOTING RULE ===
Use JSON.stringify({...}) to build every body string for pm.sendRequest.
Inside the JS object literal: SQL column VALUES must use SINGLE QUOTES ' — NEVER double quotes.
JSON.stringify() handles all escaping automatically — no manual quoting conflicts.

!! CRITICAL: In SQL VALUES (...), ALWAYS use ' for string values, NEVER " !!
  CORRECT SQL:  VALUES ('Test User', 'test@x.com', 30, 'NYC')   ← single quotes for strings
  BROKEN SQL:   VALUES ('Test User", "test@x.com", 30, 'NYC')   ← " breaks the JSON string

CORRECT — JSON.stringify with JS object (SQL strings use '):
  var body = JSON.stringify({
    "1": "DELETE FROM users WHERE email = 'test@x.com'",
    "2": "INSERT INTO users (name,email,age,city) VALUES ('Test User','test@x.com',30,'NYC')"
  });
  pm.sendRequest({ url: pm.environment.get("HELPER_HOST") + "/query", method: "POST",
    header: [{ key: "Content-Type", value: "application/json" }],
    body: { mode: "raw", raw: body }
  }, function(err, res) { console.log("seed:", res ? res.code : err); });

WRONG — SQL VALUES using " instead of ' (corrupts JSON, causes 'data: null'):
  "INSERT INTO users (...) VALUES ('Test User\","test@example.com\",30,'NYC')"  ← BROKEN

EXAMPLE — GET /data/users/email/{email} (seed data with no ID needed):
  Pre-request script:
    var body = JSON.stringify({
      "1": "DELETE FROM users WHERE email = 'seed@test.com'",
      "2": "INSERT INTO users (name,email,age,city) VALUES ('Test User','seed@test.com',30,'NYC')"
    });
    pm.sendRequest({ url: pm.environment.get("HELPER_HOST") + "/query", method: "POST",
      header: [{ key: "Content-Type", value: "application/json" }],
      body: { mode: "raw", raw: body }
    }, function(err, res) { console.log("seed:", res ? res.code : err); });
  Test script:
    pm.test("status is 200", () => pm.response.to.have.status(200));
    pm.test("success is true", () => { const b = pm.response.json(); pm.expect(b.success).to.be.true; });
    pm.test("email correct", () => { const b = pm.response.json(); pm.expect(JSON.stringify(b.data)).to.include("seed@test.com"); });
    var cleanup = JSON.stringify({ "1": "DELETE FROM users WHERE email = 'seed@test.com'" });
    pm.sendRequest({ url: pm.environment.get("HELPER_HOST") + "/query", method: "POST",
      header: [{ key: "Content-Type", value: "application/json" }],
      body: { mode: "raw", raw: cleanup }
    }, function() {});

EXAMPLE — PUT /data/users/{id} and DELETE /data/users/{id} (MUST capture auto-increment ID):
  !! NEVER hardcode the user ID (e.g. /data/users/1) — it will 400 on a fresh DB !!
  !! NEVER use pm.sendRequest in a pre-request script to capture an ID — it is async and
     Newman will fire the main request BEFORE the callback runs, so {{testUserId}} is never set !!

  CORRECT pattern: TWO setup items before the PUT/DELETE — one to INSERT, one to SELECT back the id.

  Item 1 — "Setup — Seed User for Update":
    POST "{{HELPER_HOST}}/query"
    Body: {"1":"DELETE FROM users WHERE email='upd@test.com'","2":"INSERT INTO users(name,email,age,city) VALUES('Upd','upd@test.com',30,'NYC')"}
    (no test script needed — /query returns true/false, not rows)

  Item 2 — "Setup — Get User Id for Update":
    POST "{{HELPER_HOST}}/select_query"    ← NOT /query — this one actually returns rows
    Body: {"1":"SELECT id FROM users WHERE email='upd@test.com'"}
    Test script:
      var rows = pm.response.json()["1"];
      if (rows && rows.length > 0) { pm.environment.set("testUserId", rows[0].id); }

  Item 3 — "Update User":
    PUT "{{BASE_URL}}/data/users/{{testUserId}}"    ← no prerequest needed
    Test script: assertions + cleanup via pm.sendRequest (fire-and-forget is safe in test scripts)

  The same three-item pattern applies for DELETE /data/users/{id}.

EXAMPLE — POST /data/aerospike/records (AerospikeRequest requires bins field — @NotNull):
  Request body must include "bins":
    { "key": "test-key-1", "bins": { "name": "Test", "value": 123 }, "namespace": "{{AERO_NAMESPACE}}", "set": "{{AERO_SET}}" }

EXAMPLE — Aerospike seed via helper:
  !! CRITICAL: {{VAR}} is NOT resolved inside JavaScript exec scripts !!
  Use pm.environment.get("VAR") to read env vars inside scripts.
  Pre-request:
    var body = JSON.stringify({
      "namespace": pm.environment.get("AERO_NAMESPACE"),
      "set": pm.environment.get("AERO_SET"),
      "key": "test-key-1",
      "record": {"name": "Test", "value": 123}
    });
    pm.sendRequest({ url: pm.environment.get("HELPER_HOST") + "/aerospike/set", method: "POST",
      header: [{ key: "Content-Type", value: "application/json" }],
      body: { mode: "raw", raw: body }
    }, function(err, res) { console.log("seed:", res ? res.code : err); });
  Aerospike cleanup:
    var del = JSON.stringify({
      "namespace": pm.environment.get("AERO_NAMESPACE"),
      "set": pm.environment.get("AERO_SET"),
      "key": "test-key-1"
    });
    pm.sendRequest({ url: pm.environment.get("HELPER_HOST") + "/aerospike/deleteSingle", method: "POST",
      header: [{ key: "Content-Type", value: "application/json" }],
      body: { mode: "raw", raw: del }
    }, function() {});

=== COLLECTION STRUCTURE ===
Organise requests into folders in this order:
  1. "DB Setup" — one request: POST /setdb (connects helper to MySQL)
  2. "Aerospike Setup" — one request: POST /aerospike/connect
  3. "Health" — GET /data/health
  4. "MySQL — Read" — all GET endpoints for MySQL data
  5. "MySQL — Write" — POST / PUT / DELETE for MySQL (each test seeds + cleans its own data)
  6. "Aerospike — Read" — all GET endpoints for Aerospike
  7. "Aerospike — Write" — POST / PUT / DELETE for Aerospike

=== TEST COVERAGE RULES ===
For EVERY endpoint generate at minimum:
  - Happy path: valid input → assert status 200 (or 201) AND response body fields
  - Validation error: missing required field → assert status 400
  - For path-param endpoints (GET/PUT/DELETE by ID): seed data in pre-request, clean up after

Assert on these DataResponse fields:
  pm.expect(body.success).to.be.true / false
  pm.expect(body.data).to.exist
  pm.expect(body.message).to.be.a("string")
"""


# ── Build user message from analyzed source ───────────────────────────────────

def _format_endpoint(ep: dict) -> str:
    line = f"{ep['method']:7} {ep['path']}"
    if ep["description"]:
        line += f"\n  description: {ep['description']}"
    for p in ep["params"]:
        line += f"\n  param [{p['source']}]: {p['name']} ({p['type']})"
    if ep["request_body"]:
        line += f"\n  body: {ep['request_body']}"
    return line


def _parse_controller(ctrl: dict) -> list[dict]:
    """Parse a single controller — designed for concurrent execution."""
    return parse_endpoints(ctrl["content"])


def build_context(source_data: dict) -> str:
    parts = []

    # Parse all controllers in parallel, then assemble in original order
    controllers = source_data["controllers"]
    parts.append("=== REST ENDPOINTS ===")

    if len(controllers) <= 1:
        # No parallelism needed for a single controller
        endpoint_lists = [_parse_controller(c) for c in controllers]
    else:
        endpoint_lists = [None] * len(controllers)
        with ThreadPoolExecutor(max_workers=min(len(controllers), 8)) as executor:
            futures = {
                executor.submit(_parse_controller, ctrl): idx
                for idx, ctrl in enumerate(controllers)
            }
            for future in as_completed(futures):
                idx = futures[future]
                endpoint_lists[idx] = future.result()

    for ep_list in endpoint_lists:
        for ep in (ep_list or []):
            parts.append(_format_endpoint(ep))

    # Models
    parts.append("\n=== DATA MODELS ===")
    models = parse_models(source_data["models"])
    parts.append(models_to_text(models))

    # Schema
    if source_data["schema_sql"]:
        parts.append("=== DATABASE SCHEMA ===")
        parts.append(schema_to_text(parse_schema(source_data["schema_sql"])))

    return "\n".join(parts)
