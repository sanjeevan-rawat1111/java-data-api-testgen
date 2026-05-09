# java-data-api-testgen — Interview Guide

> **How to explain every point in the project description to an interviewer**

---

## Project Description (for reference)

> *"Built java-data-api-testgen (Python/GenAI) — LLM-powered CLI pipeline that auto-generates
> Postman sanity test collections from a Java codebase; parallel module analysis, AST-based code
> parsing, and self-healing LLM retry loops (Claude Opus for generation, Sonnet for analysis) cut
> test generation time from days to minutes, with full CI/CD integration."*

---

## Point 1 — "LLM-powered CLI pipeline"

### What it is

A Python CLI tool invoked as `python -m testgen`. It has 4 subcommands:

| Command | What it does |
|---------|-------------|
| `generate` | Reads Java source → builds LLM prompt → calls LLM → saves Postman collection JSON |
| `run` | Full agentic loop: generate → start Docker services → Newman tests → LLM self-debug |
| `analyze` | Dry-run: parse Java source and print endpoints/models with no LLM call |
| `validate` | Validate an existing collection JSON for structural correctness |

### The full pipeline in one command

```
python -m testgen run
```

What happens automatically:

```
[1/4] Generate collection via LLM       (with self-healing JSON retry)
         ↓
[2/4] Start services via Docker         (MySQL + Aerospike + Spring Boot API + Flask helper)
         ↓
[3/4] Run tests via Newman              (execute all collections)
         ↓
     Tests pass? ──yes──► Save final collection + open HTML report ✔
         │ no
         ▼
[4/4] Send failures to LLM             (request name + error + response body)
      LLM fixes the failing tests
         ↓
     Re-run Newman ──► repeat up to max_debug_attempts (default: 3)
```

### Key entry-point files
- `testgen/cli.py` — all subcommands, the `cmd_run()` full pipeline loop
- `config.yaml` — LLM model, provider, retry settings, output directory

### Interview follow-up answers
- **"Why a CLI and not a UI?"** — The CLI integrates directly into CI/CD pipelines. There is also
  a Streamlit web UI at `ui/streamlit_app.py` for local exploration (Analyze / Generate / Validate
  tabs) — both options are supported.
- **"What's the output?"** — A Postman Collection v2.1.0 JSON file saved to `collections/`. Newman
  picks it up and runs it. The collection includes real pre-request setup SQL, teardown cleanup,
  and `pm.test()` assertions on every endpoint.

---

## Point 2 — "Auto-generates Postman sanity test collections from a Java codebase"

### What "auto-generate" actually means

The tool reads the `java-data-api` Spring Boot source code directly — no annotation processing,
no manual specification. It parses three things:

| What | Where in source | Parser |
|------|----------------|--------|
| REST endpoints (method, path, params, body type) | `*Controller.java` | `endpoint_parser.py` via `java_ast_parser.py` |
| Data model fields + validation annotations | `*Request.java`, `*Response.java`, `*User.java` | `model_parser.py` via `java_ast_parser.py` |
| Database schema (table names, columns, types) | `src/main/resources/sql/schema.sql` | `schema_parser.py` |

### What the LLM generates

The system prompt in `prompt_builder.py` teaches the LLM:
1. **Postman Collection v2.1.0 JSON structure** — exact schema rules, folder layout
2. **Flask helper API contracts** — `/setdb`, `/query`, `/select_query`, `/aerospike/set`,
   `/aerospike/deleteSingle` endpoints for real setup/teardown
3. **Test coverage rules** — every endpoint gets at minimum: happy path (200), validation error
   (400), path-param tests with seeded + cleaned data
4. **Critical edge cases** — how to handle async `pm.sendRequest`, how to capture auto-increment
   IDs without hardcoding, SQL quoting rules

### Generated test example

```js
// Pre-request: seed test user
var body = JSON.stringify({
  "1": "DELETE FROM users WHERE email = 'seed@test.com'",
  "2": "INSERT INTO users (name,email,age,city) VALUES ('Test User','seed@test.com',30,'NYC')"
});
pm.sendRequest({ url: pm.environment.get("HELPER_HOST") + "/query", ... });

// Test: assert response
pm.test("status 200", () => pm.response.to.have.status(200));
pm.test("success is true", () => { pm.expect(pm.response.json().success).to.be.true; });
pm.test("email correct", () => { pm.expect(JSON.stringify(pm.response.json().data)).to.include("seed@test.com"); });

// Cleanup
pm.sendRequest({ url: pm.environment.get("HELPER_HOST") + "/query", ... });
```

### Key files
- `testgen/generator/prompt_builder.py` — full system prompt + user message assembly
- `testgen/generator/collection_builder.py` — parse → validate → enrich → write + fix-ups
- `testgen/validator/validate.py` — structural validation of generated JSON

---

## Point 3 — "Parallel module analysis"

### The problem it solves

The `java-data-api` codebase can have multiple controller files (e.g., `DataController.java`,
`UserController.java`, `AerospikeController.java`). Parsing them sequentially means each one
waits for the previous to finish. For large codebases this adds up.

### Where parallelism is implemented

**Level 1 — Parallel file I/O** (`code_reader.py`)

Reading all Java source files uses a `ThreadPoolExecutor` with up to 16 workers:

```python
with ThreadPoolExecutor(max_workers=workers) as executor:
    futures = {executor.submit(_read_file, p): p for p in all_paths}
    for future in as_completed(futures):
        path, content = future.result()
        contents[path] = content
```

All controller files, model files, and `schema.sql` are read in parallel — then assembled
back in discovery order so the output is deterministic.

**Level 2 — Parallel endpoint parsing** (`prompt_builder.py`)

When there are 2+ controllers, their endpoints are parsed in parallel:

```python
with ThreadPoolExecutor(max_workers=min(len(controllers), 8)) as executor:
    futures = {
        executor.submit(_parse_controller, ctrl): idx
        for idx, ctrl in enumerate(controllers)
    }
    for future in as_completed(futures):
        idx = futures[future]
        endpoint_lists[idx] = future.result()
```

Each `_parse_controller` call runs `parse_endpoints()` which triggers the AST parser — these
are independent per-file and safe to run concurrently.

### Why this matters for the "days to minutes" claim

Before parallelism, a project with 5 controllers and 20 model files had sequential parsing.
With `ThreadPoolExecutor`, all 25 files are read and all 5 controllers are parsed at the same
time. The practical speedup on the I/O-bound file reading step is near-linear with the number
of files.

### Key files
- `testgen/analyzer/code_reader.py` — parallel file I/O, `_MAX_WORKERS = 16`
- `testgen/generator/prompt_builder.py` — parallel endpoint parsing in `build_context()`

---

## Point 4 — "AST-based code parsing"

### Why AST instead of regex

Regex parsing of Java source is fragile:
- Annotations with multiple attributes confuse pattern matchers
- Generic types like `List<UserResponse>` break naive `(\w+)` captures
- Javadoc comments in unexpected positions cause false matches
- Nested classes cause wrong context detection

AST parsing uses the actual grammar of the Java language — every annotation, parameter, and type
is correctly parsed regardless of formatting or nesting.

### How it works — `java_ast_parser.py`

Uses the `javalang` library (pure Python Java parser):

```python
tree = javalang.parse.parse(source)   # full AST of the .java file
```

**For endpoints:**
1. `tree.filter(ClassDeclaration)` — find all classes
2. For each class, check `cls.annotations` for `@RequestMapping` → extract base path
3. For each method, check `method.annotations` for `@GetMapping`, `@PostMapping`, etc.
4. For each parameter, check annotations for `@PathVariable`, `@RequestParam`, `@RequestBody`
5. Extract type names including generics via `_type_name(param.type)` — handles `List<T>`, `ResponseEntity<T>` correctly

**For models:**
1. `tree.filter(ClassDeclaration)` → walk `cls.fields`
2. For each field declaration, read `field_decl.type`, `field_decl.annotations` (e.g. `@NotNull`, `@Size(min=1)`)
3. Collect all declarators (one field line can declare multiple variables)

### Graceful fallback to regex

Both `parse_endpoints_ast()` and `parse_models_ast()` fall back to the original regex parsers
in two cases:
1. `javalang` is not installed — `ImportError` caught silently
2. The source uses syntax `javalang` can't handle (Java 16+ records, sealed classes, text blocks)
   — `Exception` caught, warning logged, regex parser used instead

```python
try:
    tree = javalang.parse.parse(source)
except Exception as exc:
    logger.warning("AST parse failed (%s) — using regex parser", exc)
    return _fallback_endpoints(source)
```

This means the tool **always works** — even on very modern Java syntax — just with lower
parsing precision for those files.

### Key files
- `testgen/analyzer/java_ast_parser.py` — full AST implementation with fallback
- `testgen/analyzer/endpoint_parser.py` — calls `parse_endpoints_ast`, falls back to `_parse_endpoints_regex`
- `testgen/analyzer/model_parser.py` — calls `parse_models_ast`, falls back to `_parse_model_fields_regex`

### Interview follow-up answers
- **"Why not use the Java compiler itself?"** — `javalang` is a pure Python parser — no JVM
  needed, no build tools, runs anywhere. The trade-off is it doesn't support the newest Java
  syntax, which is why we have the regex fallback.
- **"What specifically does AST give you that regex can't?"** — Correct generic type resolution
  (`List<UserResponse>` vs `List<String>`), correct annotation attribute extraction
  (`@Size(min=1, max=100)` reads both attributes), and correct method-vs-class context
  (no false matches from Javadoc containing similar patterns).

---

## Point 5 — "Self-healing LLM retry loops (Claude Opus for generation, Sonnet for analysis)"

### The two failure modes the healing loop handles

| Failure | What happens |
|---------|-------------|
| **Bad JSON** — LLM wraps output in markdown fences, truncates, or produces syntax errors | `_json_parse_error()` catches it, sends the error + first 800 chars back to LLM with a correction prompt |
| **Structural issues** — missing `info`, missing `item`, leaf requests with no test scripts | `structural_validator_for_healing()` catches it, same correction loop |

### The dual-model design

The key insight: the **initial generation** needs a powerful model to produce a correct, complete
Postman collection from scratch. But **correction retries** are simpler — the LLM already has a
near-correct collection, it just needs to fix one specific error. A lighter, faster model can
handle this at much lower cost.

```
Attempt 1 → uses generation model (e.g. llama-3.3-70b / Claude Opus)
             → produces the full collection

Attempt 2+ → uses analysis model  (e.g. llama-3.1-8b  / Claude Sonnet)
             → receives: previous output (broken) + exact error message
             → outputs: fixed JSON only
```

### How it's implemented in `llm_client.py`

```python
# In generate_with_healing():
healer = analysis_client or self   # analysis_client is the Sonnet model

for attempt in range(1, self.max_retries + 1):
    is_correction = attempt > 1
    active_client = healer if is_correction else self   # switch model on retry

    raw = active_client._call_with_messages(conversation)

    parse_error = _json_parse_error(raw)
    if parse_error:
        correction = _CORRECTION_PROMPT.format(error=parse_error, snippet=raw[:800])
        conversation.append({"role": "assistant", "content": raw})
        conversation.append({"role": "user",      "content": correction})
        continue  # retry with analysis model
```

The correction prompt is explicit:
> *"Your previous response could not be parsed. Here is the error: ... Please fix the issue and
> output ONLY valid JSON — no markdown fences, no explanation text, just the raw JSON object."*

### How the two models are configured in `config.yaml`

```yaml
# Generation model (heavy — produces the collection)
provider: "openai"
model: "llama-3.3-70b-versatile"
base_url: "https://api.groq.com/openai/v1"

# Analysis model (fast — self-healing corrections)
analysis_provider: "openai"
analysis_model: "llama-3.1-8b-instant"
analysis_base_url: "https://api.groq.com/openai/v1"
```

Swapping to Anthropic Claude is one config change:
```yaml
analysis_provider: "anthropic"
analysis_model: "claude-3-5-sonnet-20241022"
# set ANTHROPIC_API_KEY in .env
```

### Post-generation LLM fixes in `collection_builder.py`

Beyond the retry loop, `enrich_collection()` applies deterministic post-processing fixes
to common LLM errors — no LLM call needed for these:

| Fix | What it catches |
|-----|----------------|
| `normalize_collection_urls()` | LLM generates URL objects `{"raw":..., "host":...}` instead of plain strings |
| `_fix_hardcoded_ids()` | LLM writes `PUT /users/1` instead of `PUT /users/{{testUserId}}` |
| `_fix_sql_quoting()` | LLM mixes `"` and `'` in SQL VALUES clauses, breaking JS JSON.stringify |
| `_fix_env_vars_in_scripts()` | LLM writes `"{{AERO_NAMESPACE}}"` in JS scripts (Newman doesn't resolve `{{}}` in exec code) |
| `_fix_missing_commas()` | LLM omits commas between consecutive JS object properties |
| `_add_collection_prereq()` | Moves DB/Aerospike setup to a collection-level prerequest event |

### Key files
- `testgen/generator/llm_client.py` — `generate_with_healing()`, `make_analysis_client()`
- `testgen/generator/collection_builder.py` — `build_and_save_with_healing()`, all `_fix_*` functions

### Interview follow-up answers
- **"What if even the analysis model can't fix it?"** — After `max_retries` (default 3) exhausted,
  the loop returns the last output with a warning. The `build_and_save_with_healing()` caller
  still tries to parse and save it — so you always get *something* rather than a hard crash.
  The `self_healed` flag in the result tells you whether healing was needed.
- **"Does the conversation history grow too large for corrections?"** — The correction loop
  appends the bad output + error message to the conversation each iteration, so by retry 3 the
  context has: system + user (original) + assistant (bad) + user (error1) + assistant (bad2) +
  user (error2). For 3 retries this is manageable. The analysis model's smaller context window
  is not a problem because the correction prompt is concise.

---

## Point 6 — "Cut test generation time from days to minutes"

### The before/after

**Before (manual):** An engineer writing Postman tests manually for a Spring Boot API would:
1. Open Postman, create a collection, add folders manually
2. Write pre-request SQL for each test (seed + cleanup)
3. Write `pm.test()` assertions for each endpoint
4. Handle edge cases: PUT/DELETE need auto-increment ID capture, Aerospike needs namespace vars
5. For a 10-endpoint API with happy path + error path: **1–2 days** of test writing

**After (testgen):** `python -m testgen generate` — under 2 minutes for a 10-endpoint API.

### Why specifically "minutes":

- Parallel file I/O: all source files read simultaneously
- Parallel parsing: all controllers parsed concurrently
- Token-efficient prompt: only relevant endpoints + models sent (not the entire repo)
- `--dry-run` mode: preview the prompt without LLM call to verify parsing is correct
- `--diff` mode: when you change one endpoint, only regenerate tests for that endpoint
  (not the full suite) — merges back into the existing collection

### The incremental `--diff` mode

```bash
# Changed DataController.java — regenerate only the affected tests
python -m testgen generate --diff

# What it does:
# 1. git diff HEAD -- *.java  →  finds changed controller/model files
# 2. Parses only those files  →  smaller, faster prompt
# 3. Calls LLM for changed endpoints only
# 4. Merges new test cases into collections/java-data-api-tests.json
#    (replaces matching requests by name, appends new ones)
```

The merge logic in `collection_builder.py` → `merge_collections()` matches by folder name and
request name — so existing passing tests are never overwritten.

### Key files
- `testgen/analyzer/diff_reader.py` — `read_diff_source()` using `git diff HEAD -- *.java`
- `testgen/generator/collection_builder.py` — `merge_collections()`

---

## Point 7 — "Full CI/CD integration"

### GitHub Actions workflow

`.github/workflows/ci.yml` runs on every push/PR:

```
1. Lint (flake8 + import sort check)
2. Dry-run generation  →  python -m testgen generate --dry-run
   (verifies source parsing + prompt building works, zero LLM cost)
3. Validate existing collection (if one exists)
```

The dry-run step is the key CI gate — it runs the full Java parser pipeline and prompt builder
without spending any API tokens. If parsing breaks due to a Java source change, CI fails
immediately before any LLM call.

### Docker Compose orchestration

`docker-compose.yml` manages the full test environment:

| Service | Role |
|---------|------|
| `mysql` | Database for Spring Boot API |
| `aerospike` | Cache layer for Spring Boot API |
| `java-data-api` | The Spring Boot API under test |
| `flask-helper` | Test data setup/teardown helper (`app/app.py`) |
| `newman-runner` | Node.js Newman executor (`runner/`) |

All services start via `python -m testgen run` or manually via Docker Compose profiles.

### The Flask test helper (`app/`)

This is a small Flask API that runs inside Docker and exposes:
- `POST /setdb` — connects to MySQL with provided credentials
- `POST /query` — executes fire-and-forget SQL (INSERT/DELETE/UPDATE) — returns `true/false`
- `POST /select_query` — executes SELECT and returns actual rows
- `POST /aerospike/connect`, `/aerospike/set`, `/aerospike/deleteSingle` — Aerospike operations

Newman's pre-request scripts call these endpoints to seed exactly the data each test needs
and clean up after. This makes tests **idempotent** — they don't depend on pre-existing data.

### Key files
- `.github/workflows/ci.yml` — CI pipeline with dry-run gate
- `docker-compose.yml` — full service orchestration
- `app/app.py`, `app/helpers.py` — Flask test helper
- `runner/src/runner.js` — Newman wrapper that generates HTML reports

---

## Quick-fire Interview Q&A

| Question | Answer |
|----------|--------|
| **"What tech stack?"** | Python 3.9+, `javalang` (AST), `concurrent.futures` (parallelism), `PyYAML` (config), `openai` + `anthropic` SDKs, Google `genai` SDK, `streamlit` (UI), Newman/Node.js (test runner) |
| **"Which LLMs are supported?"** | Gemini Flash (default, free), Groq (Llama-3.3-70b, free tier), OpenAI (GPT-4o), Anthropic (Claude Opus/Sonnet), Ollama (local). All configured via `config.yaml` + `OPENAI_API_KEY` env var. The OpenAI SDK's `base_url` parameter makes every provider work through one interface. |
| **"How do you handle LLM rate limits?"** | Exponential backoff in `generate()`: wait = `retry_delay * 2^(attempt-1)`. Default 2s, 4s, 8s across 3 attempts. For Groq free tier, `max_tokens` is capped at 7500 to stay within their rate limit. |
| **"What if the Java source uses Spring WebFlux or non-standard annotations?"** | The AST parser looks for `@GetMapping`, `@PostMapping`, `@PutMapping`, `@DeleteMapping`. If the source uses `@RequestMapping(method=GET)` the annotation value extractor handles it. For completely non-standard frameworks, the regex fallback fires. |
| **"How are tests kept idempotent?"** | Every test seeds its own data in a pre-request script and deletes it in the test cleanup. The `_fix_hardcoded_ids` step in `collection_builder.py` ensures no test hardcodes DB-generated IDs. |
| **"How does the Newman runner report results?"** | `runner/src/runner.js` uses Newman programmatically, generates an HTML report via `newman-reporter-htmlextra`, and streams pass/fail counts back to `newman_runner.py`. The Python side reads `result.failed` to decide whether to trigger the LLM debug loop. |
| **"What's the Streamlit UI for?"** | Local exploration only — engineers who prefer a GUI can use the Analyze tab to preview parsed endpoints, the Generate tab to configure model + key and click Generate, and the Validate tab to check collection structure. The core pipeline is identical to the CLI. |
