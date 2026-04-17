"""
llm_client.py — LLM provider wrapper with self-healing retry logic.

Self-healing covers two failure modes:
  1. API / network errors     — exponential backoff, up to max_retries
  2. Bad JSON / structure     — sends the error back to the LLM with a
                                correction prompt and retries

Supported providers (configured via config.yaml):
  - openai   (default) — GPT-4o, GPT-4-turbo, GPT-3.5-turbo
  - anthropic           — Claude 3.5 Sonnet, Claude 3 Haiku

Environment variables:
  OPENAI_API_KEY    — required for openai provider
  ANTHROPIC_API_KEY — required for anthropic provider
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

# Auto-load .env if present (so OPENAI_API_KEY is available without manual export)
def _load_dotenv():
    env_path = Path(".env")
    if not env_path.exists():
        env_path = Path(__file__).parent.parent.parent / ".env"
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())

_load_dotenv()

logger = logging.getLogger(__name__)

# Prompt injected on retry to tell the LLM what went wrong
_CORRECTION_PROMPT = """\
Your previous response could not be parsed. Here is the error:

  {error}

Your previous output (first 800 chars):
  {snippet}

Please fix the issue and output ONLY valid JSON — no markdown fences, \
no explanation text, just the raw JSON object.
"""


class LLMClient:
    def __init__(self, config: dict):
        self._config     = config
        self.provider    = config.get("provider", "openai").lower()
        self.model       = config.get("model", "gpt-4o")
        self.temperature = float(config.get("temperature", 0.2))
        self.max_tokens  = int(config.get("max_tokens", 4096))
        self.max_retries = int(config.get("max_retries", 3))
        self.retry_delay = float(config.get("retry_delay_seconds", 2.0))
        self._client     = self._init_client()

    # ── Init ────────────────────────────────────────────────────────────────

    def _init_client(self) -> Any:
        if self.provider == "gemini":
            # Native Google Generative AI SDK — uses API key only, no ADC conflict
            try:
                import google.generativeai as genai
            except ImportError:
                raise ImportError("Install: pip install google-generativeai")
            api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise EnvironmentError(
                    "GOOGLE_API_KEY (or OPENAI_API_KEY) not set.\n"
                    "  Get a free key at https://aistudio.google.com/apikey\n"
                    "  Then set in .env:  GOOGLE_API_KEY=AIza..."
                )
            genai.configure(api_key=api_key)
            return genai.GenerativeModel(self.model)

        elif self.provider == "openai":
            try:
                from openai import OpenAI
            except ImportError:
                raise ImportError("Install openai: pip install openai")
            api_key = os.environ.get("OPENAI_API_KEY")
            if not api_key:
                raise EnvironmentError(
                    "OPENAI_API_KEY is not set.\n"
                    "  Free option: set provider: gemini in config.yaml and get a\n"
                    "  Google AI Studio key at https://aistudio.google.com/apikey"
                )
            base_url = self._config.get("base_url") or os.environ.get("OPENAI_BASE_URL") or None
            return OpenAI(api_key=api_key, base_url=base_url)

        elif self.provider == "anthropic":
            try:
                import anthropic
            except ImportError:
                raise ImportError("Install anthropic: pip install anthropic")
            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                raise EnvironmentError("ANTHROPIC_API_KEY not set")
            return anthropic.Anthropic(api_key=api_key)

        else:
            raise ValueError(f"Unknown provider: {self.provider}. Valid: gemini, openai, anthropic")

    # ── Public: generate with API-level retry (backoff) ─────────────────────

    def generate(self, system_prompt: str, user_message: str) -> str:
        """
        Call the LLM with exponential-backoff retry on API/network failures.
        Returns the raw text response.
        """
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                logger.debug("LLM call attempt %d/%d", attempt, self.max_retries)
                if self.provider == "gemini":
                    return self._gemini_call(system_prompt, user_message)
                elif self.provider == "openai":
                    return self._openai_call(system_prompt, user_message)
                elif self.provider == "anthropic":
                    return self._anthropic_call(system_prompt, user_message)
            except Exception as e:
                last_error = e
                if attempt < self.max_retries:
                    wait = self.retry_delay * (2 ** (attempt - 1))   # exponential backoff
                    logger.warning(
                        "LLM API error on attempt %d: %s — retrying in %.1fs",
                        attempt, e, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error("LLM API failed after %d attempts: %s", self.max_retries, e)

        raise RuntimeError(f"LLM call failed after {self.max_retries} attempts: {last_error}")

    # ── Public: self-healing generate (fixes bad output) ────────────────────

    def generate_with_healing(
        self,
        system_prompt: str,
        user_message: str,
        validate_fn=None,
    ) -> tuple[str, int]:
        """
        Generate and self-heal if the output is not valid JSON or fails
        structural validation.

        Args:
            system_prompt: LLM system prompt
            user_message:  LLM user message
            validate_fn:   Optional callable(str) -> list[str] of errors.
                           If provided, a non-empty error list triggers a
                           correction retry even when JSON is technically valid.

        Returns:
            (raw_output, attempts_used)
        """
        conversation = [
            {"role": "system",  "content": system_prompt},
            {"role": "user",    "content": user_message},
        ]

        last_output = ""
        for attempt in range(1, self.max_retries + 1):
            logger.info("Generation attempt %d/%d", attempt, self.max_retries)

            # Get LLM response for current conversation
            raw = self._call_with_messages(conversation)
            last_output = raw

            # ── Check 1: is it parseable JSON? ──────────────────────────────
            parse_error = _json_parse_error(raw)
            if parse_error:
                logger.warning("Attempt %d: JSON parse error — %s", attempt, parse_error)
                if attempt < self.max_retries:
                    correction = _CORRECTION_PROMPT.format(
                        error=parse_error,
                        snippet=raw[:800],
                    )
                    # Add assistant response + correction as next user turn
                    conversation.append({"role": "assistant", "content": raw})
                    conversation.append({"role": "user",      "content": correction})
                    time.sleep(self.retry_delay)
                    continue
                break   # exhausted retries

            # ── Check 2: custom structural validation ───────────────────────
            if validate_fn:
                errors = validate_fn(raw)
                if errors:
                    logger.warning(
                        "Attempt %d: structural issues — %s", attempt, "; ".join(errors)
                    )
                    if attempt < self.max_retries:
                        correction = _CORRECTION_PROMPT.format(
                            error="Structural validation failed:\n  " + "\n  ".join(errors),
                            snippet=raw[:800],
                        )
                        conversation.append({"role": "assistant", "content": raw})
                        conversation.append({"role": "user",      "content": correction})
                        time.sleep(self.retry_delay)
                        continue
                    break

            # ── All checks passed ────────────────────────────────────────────
            logger.info("Generation succeeded on attempt %d", attempt)
            return raw, attempt

        logger.warning(
            "Self-healing exhausted after %d attempts — returning last output", self.max_retries
        )
        return last_output, self.max_retries

    # ── Internal LLM calls ────────────────────────────────────────────────────

    def _gemini_call(self, system_prompt: str, user_message: str) -> str:
        prompt = f"{system_prompt}\n\n{user_message}"
        response = self._client.generate_content(
            prompt,
            generation_config={
                "temperature": self.temperature,
                "max_output_tokens": self.max_tokens,
                "response_mime_type": "application/json",
            },
        )
        return response.text


    def _call_with_messages(self, messages: list[dict]) -> str:
        """Call the provider with a full conversation history."""
        if self.provider == "gemini":
            # Flatten conversation into a single prompt for Gemini
            parts = []
            for m in messages:
                role = m["role"].upper()
                if role == "SYSTEM":
                    parts.append(m["content"])
                else:
                    parts.append(m["content"])
            prompt = "\n\n".join(parts)
            response = self._client.generate_content(
                prompt,
                generation_config={
                    "temperature": self.temperature,
                    "max_output_tokens": self.max_tokens,
                    "response_mime_type": "application/json",
                },
            )
            return response.text

        elif self.provider == "openai":
            response = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                response_format={"type": "json_object"},
                messages=messages,
            )
            return response.choices[0].message.content

        elif self.provider == "anthropic":
            import anthropic
            # Anthropic: system is separate, messages are user/assistant only
            system = next((m["content"] for m in messages if m["role"] == "system"), "")
            turns  = [m for m in messages if m["role"] != "system"]
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=turns,
            )
            return response.content[0].text

    def _openai_call(self, system_prompt: str, user_message: str) -> str:
        return self._call_with_messages([
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ])

    def _anthropic_call(self, system_prompt: str, user_message: str) -> str:
        return self._call_with_messages([
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": user_message},
        ])

    # ── Utility ──────────────────────────────────────────────────────────────

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Rough estimate: ~4 chars per token."""
        return len(text) // 4


# ── Helpers ──────────────────────────────────────────────────────────────────

def _json_parse_error(text: str) -> str | None:
    """Return the parse error message if text is not valid JSON, else None."""
    import re
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned.strip())
    try:
        json.loads(cleaned)
        return None
    except json.JSONDecodeError as e:
        return str(e)
