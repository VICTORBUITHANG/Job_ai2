from __future__ import annotations

import json
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


class LocalLLMError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, base_url: str, model: str, timeout_seconds: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    def generate_json(self, prompt: dict[str, Any]) -> dict[str, Any]:
        if not self.model:
            raise LocalLLMError("LOCAL_LLM_MODEL is not set.")
        payload = {
            "model": self.model,
            "prompt": json.dumps(prompt, ensure_ascii=True),
            "stream": False,
            "format": "json",
            "options": {
                "temperature": 0,
            },
        }
        request = Request(
            f"{self.base_url}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except URLError as exc:
            raise LocalLLMError(f"Could not reach Ollama at {self.base_url}: {exc}") from exc
        except TimeoutError as exc:
            raise LocalLLMError(f"Ollama request timed out after {self.timeout_seconds}s.") from exc

        try:
            envelope = json.loads(raw)
            content = envelope.get("response", "")
            if isinstance(content, dict):
                return content
            return json.loads(str(content))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LocalLLMError("Ollama did not return valid JSON.") from exc
