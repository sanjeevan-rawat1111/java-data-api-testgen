"""
Streamlit UI for java-data-api-testgen.

Run:
  streamlit run ui/streamlit_app.py
"""

import json
import os
import sys
from pathlib import Path

import streamlit as st
import yaml

# ── make testgen importable ───────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from testgen.analyzer.code_reader      import read_source
from testgen.analyzer.endpoint_parser  import parse_endpoints
from testgen.analyzer.model_parser     import parse_models, models_to_text
from testgen.analyzer.schema_parser    import parse_schema, schema_to_text
from testgen.generator.prompt_builder  import SYSTEM_PROMPT, build_context
from testgen.generator.llm_client      import LLMClient
from testgen.generator.collection_builder import build_and_save_with_healing
from testgen.validator.validate        import load_collection, validate_collection


# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="java-data-api testgen",
    page_icon="🤖",
    layout="wide",
)

st.title("🤖 java-data-api testgen")
st.caption("AI-powered Postman test collection generator")


# ── Load default config ───────────────────────────────────────────────────────

@st.cache_data
def load_config():
    cfg_path = ROOT / "config.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            return yaml.safe_load(f)
    return {}

default_cfg = load_config()


# ── Sidebar — settings ────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Settings")

    repo_path = st.text_input(
        "java-data-api path",
        value=default_cfg.get("java_data_api_path", "../java-data-api"),
    )
    api_key = st.text_input(
        "API Key (OPENAI_API_KEY)",
        value=os.environ.get("OPENAI_API_KEY", ""),
        type="password",
    )
    model = st.text_input("Model", value=default_cfg.get("model", "gemini-1.5-flash"))
    base_url = st.text_input(
        "Base URL",
        value=default_cfg.get("base_url", "https://generativelanguage.googleapis.com/v1beta/openai/"),
    )
    feature_name = st.text_input(
        "Collection name",
        value=default_cfg.get("default_feature_name", "java-data-api-tests"),
    )
    max_retries = st.slider("Self-healing max retries", 1, 5, default_cfg.get("max_retries", 3))

    st.divider()
    st.caption("Free key: [aistudio.google.com](https://aistudio.google.com/apikey)")


# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_analyze, tab_generate, tab_validate = st.tabs(["🔍 Analyze", "✨ Generate", "✅ Validate"])


# ── Tab 1: Analyze ────────────────────────────────────────────────────────────

with tab_analyze:
    st.subheader("API Surface")
    st.write("Parse the Java source and preview what the LLM will receive.")

    if st.button("Analyze source", type="primary"):
        resolved = str((ROOT / repo_path).resolve())
        with st.spinner(f"Reading {resolved} ..."):
            try:
                source = read_source(resolved)
                st.success(
                    f"Found {len(source['controllers'])} controller(s), "
                    f"{len(source['models'])} model(s), "
                    f"schema: {'yes' if source['schema_sql'] else 'no'}"
                )

                # Endpoints
                with st.expander("Endpoints", expanded=True):
                    for ctrl in source["controllers"]:
                        for ep in parse_endpoints(ctrl["content"]):
                            cols = st.columns([1, 3, 4])
                            cols[0].code(ep["method"])
                            cols[1].code(ep["path"])
                            if ep["description"]:
                                cols[2].caption(ep["description"][:120])

                # Models
                with st.expander("Models"):
                    st.text(models_to_text(parse_models(source["models"])))

                # DB schema
                if source["schema_sql"]:
                    with st.expander("DB Schema"):
                        st.text(schema_to_text(parse_schema(source["schema_sql"])))

                # Token estimate
                user_msg = build_context(source)
                token_est = LLMClient.estimate_tokens(SYSTEM_PROMPT + user_msg)
                st.info(f"Estimated prompt tokens: ~{token_est:,}")

                st.session_state["source_data"] = source

            except Exception as e:
                st.error(f"Error: {e}")


# ── Tab 2: Generate ───────────────────────────────────────────────────────────

with tab_generate:
    st.subheader("Generate Collection")

    col_btn, col_dry = st.columns([1, 1])
    dry_run = col_dry.toggle("Dry-run (no LLM call)", value=False)
    run_gen = col_btn.button("Generate", type="primary", disabled=(not api_key and not dry_run))

    if not api_key and not dry_run:
        st.warning("Enter an API key in the sidebar to generate.")

    if run_gen:
        resolved = str((ROOT / repo_path).resolve())

        with st.spinner("Reading source..."):
            source = read_source(resolved)
            st.session_state["source_data"] = source

        user_message = build_context(source)

        if dry_run:
            st.subheader("System Prompt (preview)")
            st.code(SYSTEM_PROMPT[:1500] + "\n...", language="text")
            st.subheader("User Message (preview)")
            st.code(user_message[:3000] + ("\n..." if len(user_message) > 3000 else ""), language="text")
            token_est = LLMClient.estimate_tokens(SYSTEM_PROMPT + user_message)
            st.info(f"Estimated tokens: ~{token_est:,} | LLM call skipped.")
        else:
            cfg = {
                "provider":      "openai",
                "model":         model,
                "base_url":      base_url,
                "api_key":       api_key,
                "max_retries":   max_retries,
                "retry_delay_seconds": default_cfg.get("retry_delay_seconds", 2.0),
                "temperature":   default_cfg.get("temperature", 0.2),
                "max_tokens":    default_cfg.get("max_tokens", 8192),
            }
            output_dir = str(ROOT / "collections")

            progress = st.progress(0, text="Calling LLM...")
            status_box = st.empty()

            try:
                client = LLMClient(cfg)
                result = build_and_save_with_healing(
                    llm_client=client,
                    system_prompt=SYSTEM_PROMPT,
                    user_message=user_message,
                    feature_name=feature_name,
                    output_dir=output_dir,
                )
                progress.progress(100, text="Done!")

                healed = " (self-healed)" if result.get("self_healed") else ""
                st.success(
                    f"✔ Saved to `{result['output_path']}`{healed}  |  "
                    f"{result['total_requests']} requests  |  "
                    f"{result['attempts']}/{max_retries} attempts"
                )

                if result["warnings"]:
                    for w in result["warnings"]:
                        st.warning(w)

                # Show preview of collection
                with open(result["output_path"]) as f:
                    collection_json = json.load(f)
                with st.expander("Generated collection JSON (preview)", expanded=False):
                    st.json(collection_json)

                # Download button
                st.download_button(
                    "⬇ Download collection",
                    data=json.dumps(collection_json, indent=2),
                    file_name=f"{feature_name}.json",
                    mime="application/json",
                )

            except Exception as e:
                progress.empty()
                st.error(f"Generation failed: {e}")


# ── Tab 3: Validate ───────────────────────────────────────────────────────────

with tab_validate:
    st.subheader("Validate a Collection")
    st.write("Upload a Postman collection JSON to check its structure.")

    uploaded = st.file_uploader("Upload collection JSON", type="json")

    if uploaded:
        try:
            data = json.load(uploaded)
            issues = validate_collection(data)
            if not issues:
                st.success(f"✔ Valid Postman collection — no issues found.")
                total_reqs = sum(1 for item in data.get("item", []) if "request" in item)
                st.metric("Top-level items", len(data.get("item", [])))
            else:
                st.error(f"{len(issues)} issue(s) found:")
                for issue in issues:
                    st.warning(issue)
        except Exception as e:
            st.error(f"Could not parse JSON: {e}")

    # Also let user validate any file already in collections/
    collections_dir = ROOT / "collections"
    existing = sorted(collections_dir.glob("*.json"))
    if existing:
        st.divider()
        st.write("Or validate a generated collection:")
        selected = st.selectbox("Select collection", [f.name for f in existing])
        if st.button("Validate selected"):
            try:
                col = load_collection(str(collections_dir / selected))
                issues = validate_collection(col)
                if not issues:
                    st.success(f"✔ {selected} is valid.")
                else:
                    for issue in issues:
                        st.warning(issue)
            except Exception as e:
                st.error(str(e))
