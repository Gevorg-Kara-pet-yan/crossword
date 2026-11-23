import os
import json
import asyncio
import re
import anthropic
import requests
from abc import ABC, abstractmethod
from dotenv import load_dotenv
from logging_config import get_logger

load_dotenv()
logger = get_logger("ai_provider")

# ─── Shared helpers ────────────────────────────────────────────────

def normalize(text: str) -> str:
    return text.strip().upper()

def clean_json_response(raw: str) -> dict | list:
    clean = raw.replace("```json", "").replace("```", "").replace("`", "").strip()
    # Try object first
    start = clean.find("{")
    if start != -1:
        end = clean.rfind("}") + 1
        return json.loads(clean[start:end])
    # Fallback to array
    start = clean.find("[")
    end = clean.rfind("]") + 1
    return json.loads(clean[start:end])

# ─── Abstract base ─────────────────────────────────────────────────

class AIProvider(ABC):

    @abstractmethod
    async def ask_async(self, prompt: str) -> str:
        """Վերադարձնում է raw string պատասխան"""
        ...

    def ask_sync(self, prompt: str) -> str:
        return asyncio.get_event_loop().run_until_complete(self.ask_async(prompt))

    async def generate_questions(self, topic: str, count: int, description: str = "") -> list[dict]:
        prompt = f"""
Ստեղծիր խաչբառ '{topic}' թեմայով։

Լրացուցիչ նկարագրություն՝ {description}

ՊԱՀԱՆՋՆԵՐ՝
1. Գրիր կարճ ՆԿԱՐԱԳՐՈՒԹՅՈՒՆ (2-3 նախադասություն)
2. Ստեղծիր {count} հարց և պատասխան

ՀԱՐՑԵՐԻ ՊԱՀԱՆՋՆԵՐ՝
- Պատասխանները մեկ բառով
- Հայերեն
- ՄԵԾԱՏԱՌ

ՎԵՐԱԴԱՐՁՐՈՒ ՄԻԱՅՆ JSON՝
{{
  "description": "...",
  "questions": [
    {{"q": "հարց", "a": "ՊԱՏԱՍԽԱՆ"}}
  ]
}}
"""
        raw = await self.ask_async(prompt)
        try:
            parsed = clean_json_response(raw)
            items = parsed.get("questions", []) if isinstance(parsed, dict) else parsed
        except Exception as e:
            logger.error(
                "ai_json_parse_failed",
                extra={"Operation": "GenerateQuestions", "Exception": type(e).__name__,
                       "RawLength": len(raw or ""), "RawTail": (raw or "")[-150:]},
            )
            return []

        result = []
        for item in items or []:
            if not isinstance(item, dict):
                continue
            q = str(item.get("q") or item.get("question") or "").strip()
            a = normalize(str(item.get("a") or item.get("answer") or ""))
            if q and len(a) >= 3:
                result.append({"q": q, "a": a})
        return result

    def call_for_json(self, prompt: str) -> list | dict:
        """Sync կանչ, JSON վերադարձ (spell-check route-ի համար)"""
        raw = self.ask_sync(prompt)
        try:
            return clean_json_response(raw)
        except Exception:
            match = re.search(r"\[.*\]", raw, re.S)
            if match:
                return json.loads(match.group())
            return []

# ─── Claude (Anthropic SDK) ────────────────────────────────────────

class ClaudeProvider(AIProvider):

    def __init__(self):
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")
        self._max_tokens = int(os.getenv("ANTHROPIC_MAX_TOKENS", "8000"))

    async def ask_async(self, prompt: str) -> str:
        message = await asyncio.to_thread(
            self._client.messages.create,
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if message.stop_reason == "max_tokens":
            logger.warning(
                "claude_response_truncated",
                extra={"Operation": "ClaudeAsk", "Model": self._model, "MaxTokens": self._max_tokens},
            )
        return "".join(
            b.text for b in message.content if getattr(b, "type", "") == "text"
        ).strip()

# ─── Custom / DSH AI ──────────────────────────────────────────────

class CustomProvider(AIProvider):

    def __init__(self):
        self._url = os.getenv("API_URL", "http://localhost:5000/api/ask")
        self._key = os.getenv("X_API_KEY", "")
        if not self._key:
            raise RuntimeError("X_API_KEY is not set in .env")

    async def ask_async(self, prompt: str) -> str:
        headers = {
            "X-API-Key": self._key,
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = json.dumps({"message": prompt}, ensure_ascii=False).encode("utf-8")
        response = await asyncio.to_thread(
            requests.post,
            self._url,
            headers=headers,
            data=payload,
            timeout=120,
            verify=False
        )
        response.encoding = "utf-8"
        raw = response.text
        print("🟢 Custom AI RAW:", raw[:200])
        # Unwrap {"ok": true, "answer": "..."} եթե կա
        try:
            outer = json.loads(raw)
            if isinstance(outer, dict) and "answer" in outer:
                return outer["answer"]
        except Exception:
            pass
        return raw

# ─── Factory ───────────────────────────────────────────────────────

def get_provider(choice: str = None) -> AIProvider:
    if choice:
        choice = choice.strip().lower()
    else:
        choice = os.getenv("AI_PROVIDER", "claude").strip().lower()

    if choice == "claude":
        return ClaudeProvider()
    elif choice in ["custom", "dshh"]:
        return CustomProvider()
    else:
        raise ValueError(f"Անհայտ AI_PROVIDER: '{choice}'")