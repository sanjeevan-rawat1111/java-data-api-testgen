# java-data-api-testgen

AI-powered test automation framework for [java-data-api](../java-data-api).

LLM reads the Java source code → generates Postman test collections with real setup/teardown → Newman runs them against the live API in Docker.

## How It Works

```
java-data-api/
└── src/main/java/...          Java controllers, models, schema.sql
         │
         ▼
  testgen/analyzer/            Parse: endpoints, models, DB schema
         │
         ▼
  testgen/generator/           Build LLM prompt (with Flask helper context)
         │                     Call LLM (Gemini / OpenAI / Groq / Ollama)
         │                     Self-healing retry on bad JSON output
         ▼
  collections/                 Generated Postman collection JSON
  (e.g. java-data-api-tests.json)
         │
         ▼
  docker-compose up            MySQL + Aerospike + java-data-api
                               + flask-helper (setup/teardown)
                               + newman-runner (executes collections)
         │
         ▼
  runner/reports/              HTML test report
```

## Quick Start

### 1. Get a free API key

Go to [aistudio.google.com/apikey](https://aistudio.google.com/apikey) → sign in with Google → Create API key.

### 2. Setup

```sh
cd java-data-api-testgen
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.sample .env
# Paste your key: OPENAI_API_KEY=AIza...
```

### 3. Run the full pipeline (single command)

```sh
python -m testgen run
```

This runs the full **agentic loop** automatically:

```
[1/4] Generate collection via LLM  (with self-healing JSON retry)
         ↓
[2/4] Start services via Docker    (MySQL + Aerospike + API + Flask helper)
         ↓
[3/4] Run tests via Newman         (execute all collections)
         ↓
     Tests pass? ──yes──▶ Save final collection + open HTML report ✔
         │ no
         ▼
[4/4] Send failures to LLM         (request name + error + response body)
      LLM fixes the failing tests
         ↓
     Re-run Newman  ──▶  repeat up to max_debug_attempts (default: 3)
         ↓
     Save best collection + open HTML report
```

### Or run steps individually

```sh
# Verify parsing (no LLM call)
python -m testgen analyze

# Preview prompt (no LLM call)
python -m testgen generate --dry-run

# Generate only → saves to collections/
python -m testgen generate

# Reuse existing collection, just run tests
python -m testgen run --skip-generate
```

## Project Structure

```
java-data-api-testgen/
│
├── testgen/                   Python package — AI core
│   ├── analyzer/              Parse Java source (endpoints, models, schema)
│   │   ├── code_reader.py
│   │   ├── endpoint_parser.py
│   │   ├── model_parser.py
│   │   └── schema_parser.py
│   ├── generator/             LLM integration
│   │   ├── prompt_builder.py  System prompt + user context (includes Flask helper API)
│   │   ├── llm_client.py      Async client, failover, self-healing retry loop
│   │   └── collection_builder.py  Parse → validate → save
│   ├── validator/
│   │   └── validate.py        Structural validation of Postman JSON
│   └── cli.py                 Entry point: python -m testgen
│
├── app/                       Flask test helper — MySQL + Aerospike setup/teardown
│   ├── app.py
│   ├── helpers.py             /setdb /query /aerospike/* endpoints
│   ├── mysql_client.py
│   ├── aerospike_client.py
│   ├── requirements.txt
│   └── Dockerfile
│
├── runner/                    Newman runner — executes collections, generates HTML report
│   ├── src/
│   │   ├── runner.js
│   │   └── cli.js
│   ├── package.json
│   ├── reports/               Generated HTML reports
│   └── Dockerfile
│
├── ui/
│   └── streamlit_app.py       Visual UI — Analyze / Generate / Validate tabs
│
├── collections/               AI-generated Postman collections (testgen writes here)
├── env/
│   └── environment.json       Postman env vars (BASE_URL, HELPER_HOST, DB creds, etc.)
│
├── docker-compose.yml         Orchestrates everything
├── config.yaml                LLM model, base_url, output path
├── requirements.txt
├── .env.sample
└── README.md
```

## What the LLM Generates

The generated collections use the Flask helper for **real setup and teardown** — not just raw API calls:

- **DB Setup folder** — connects Flask helper to MySQL before tests run
- **Aerospike Setup folder** — connects Flask helper to Aerospike
- **Per-test pre-request scripts** — seed exactly the data each test needs using `/query`
- **Per-test cleanup** — delete seeded data after assertions using `/query` or `/aerospike/deleteSingle`
- **Assertions on DataResponse fields** — `success`, `data`, `message`

Example generated test (GET /data/users/email/{email}):
```js
// Pre-request: seed test user
pm.sendRequest({ url: HELPER + "/query", method: "POST",
  body: { mode: "raw", raw: JSON.stringify({"1": "INSERT INTO users ...test_user@test.com..."}) }
}, () => {});

// Test: assert response
pm.test("status 200", () => pm.response.to.have.status(200));
pm.test("correct user returned", () => {
  pm.expect(pm.response.json().data).to.include("test_user@test.com");
});

// Cleanup
pm.sendRequest({ url: HELPER + "/query", method: "POST",
  body: { mode: "raw", raw: JSON.stringify({"1": "DELETE FROM users WHERE email='test_user@test.com'"}) }
}, () => {});
```

## Streamlit UI

Prefer a visual interface? Run the Streamlit app instead of the CLI:

```sh
streamlit run ui/streamlit_app.py
```

Opens at `http://localhost:8501` with three tabs:
- **Analyze** — parse the Java source, preview endpoints/models/schema, see token estimate
- **Generate** — configure model + key, click Generate, download the collection, or dry-run to preview the prompt
- **Validate** — upload or select a generated collection and check its structure

## CLI Reference

```
python -m testgen run                             Full pipeline: generate → services → tests → report
python -m testgen run --skip-generate            Reuse existing collection, just run tests
python -m testgen generate                        Generate full collection → collections/
python -m testgen generate --dry-run              Preview prompt, skip LLM
python -m testgen generate --feature Name         Custom collection name
python -m testgen generate --diff                 Incremental: only regenerate changed endpoints
python -m testgen generate --diff --base main     Diff against a specific branch/ref
python -m testgen analyze                         Print full parsed API surface
python -m testgen analyze --diff                  Show only changed endpoints
python -m testgen validate collections/X.json     Validate a generated collection
```

### Incremental generation (`--diff`)

When you change an endpoint in `java-data-api`, you don't need to regenerate everything.
`--diff` detects exactly which Java files changed, sends only those to the LLM, and
**merges** the new test cases back into the existing collection:

```sh
# You modified DataController.java — regenerate only the affected tests
python -m testgen generate --diff

# What it does:
# 1. git diff HEAD -- *.java  →  finds changed controller files
# 2. Parses only those files  →  smaller, faster prompt
# 3. Calls LLM for changed endpoints only
# 4. Merges new test cases into collections/java-data-api-tests.json
#    (replaces matching requests by name, appends new ones)
```

## LLM Configuration

Default: **Google Gemini Flash** (free, no credit card).

Switch in `config.yaml` + `.env`:

```yaml
# Groq (free tier, very fast)
model: "llama-3.1-70b-versatile"
base_url: "https://api.groq.com/openai/v1"
# OPENAI_API_KEY=gsk_your_groq_key

# OpenAI (paid, best quality)
model: "gpt-4o"
base_url: "https://api.openai.com/v1"
# OPENAI_API_KEY=sk-...

# Ollama (local, no key needed)
model: "llama3.1"
base_url: "http://localhost:11434/v1"
# OPENAI_API_KEY=ollama
```
