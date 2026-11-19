"""DeepSeek V4.1-Flash transport for the document extraction contracts."""

import http.client
import json
import os
from pathlib import Path

from extraction_prompts import _INSTRUCTION, MATERIAL_INSTRUCTION, _model_input

from doccase_preprocessing.extraction_agent import AgentServiceError
from doccase_preprocessing.group_context_agent import CONTEXT_INSTRUCTION
from doccase_preprocessing.object_agent import OBJECT_INSTRUCTION

MODEL = "deepseek-flash"
MODEL_NAME = "DeepSeek-V4.1-Flash"
HOST = "api.deepseek.com"
THINKING = "disabled"
KEY_FILE = Path(__file__).resolve().parents[1] / "private/config/deepseek.local.json"


class DeepSeekExtractionAgent:
    def __init__(self, api_key):
        self._api_key = api_key

    def __call__(self, request, *, timeout_seconds):
        payload = _model_input(request)
        if not payload["evidence"]:
            return {"requirement_id": request["requirement_id"], "candidate": None}
        instruction = _INSTRUCTION
        if payload["field_name"] == "destination":
            instruction = instruction.replace(
                "value 必须是正整数", "value 必须是证据中的目的地字符串"
            )
        return self._complete(instruction, payload, timeout_seconds)

    def extract_material(self, request, *, timeout_seconds):
        return self._complete(
            MATERIAL_INSTRUCTION,
            {k: request[k] for k in ("source_kind", "requirements", "evidence")},
            timeout_seconds,
        )

    def extract_group_context(self, request, *, timeout_seconds):
        return self._complete(
            CONTEXT_INSTRUCTION,
            {k: request[k] for k in ("requirements", "sources")},
            timeout_seconds,
        )

    def extract_objects(self, request, *, timeout_seconds):
        return self._complete(
            OBJECT_INSTRUCTION,
            {key: request[key] for key in ("object_id", "object_kind", "requirements", "evidence")},
            timeout_seconds,
        )

    def _complete(self, instruction, payload, timeout_seconds):
        body = {
            "model": MODEL,
            "thinking": {"type": THINKING},
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": instruction + "\n仅返回 JSON 对象。"},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        connection = http.client.HTTPSConnection(HOST, timeout=timeout_seconds)
        try:
            connection.request(
                "POST",
                "/chat/completions",
                json.dumps(body, ensure_ascii=False).encode("utf-8"),
                {
                    "Authorization": "Bearer " + self._api_key,
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            response = connection.getresponse()
            if response.status in {401, 403}:
                raise AgentServiceError("deepseek_auth_failed")
            if not 200 <= response.status < 300:
                raise AgentServiceError("deepseek_http_error")
            data = json.loads(response.read())
            if data["model"] != MODEL:
                raise AgentServiceError("deepseek_unexpected_model")
            choice = data["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise AgentServiceError("deepseek_incomplete_response")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise AgentServiceError("deepseek_empty_response")
            return content
        except TimeoutError:
            raise
        except (OSError, http.client.HTTPException, ValueError, KeyError, IndexError, TypeError):
            raise AgentServiceError("deepseek_unavailable_or_invalid_response") from None
        finally:
            connection.close()


def load_deepseek_agent(key_file=KEY_FILE):
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key and key_file.is_file():
        try:
            config = json.loads(key_file.read_text(encoding="utf-8-sig"))
            key = config.get("api_key", "").strip()
        except (OSError, ValueError, AttributeError):
            raise ValueError(
                "Invalid DeepSeek configuration; expected a JSON api_key string"
            ) from None
    # MiniMax subscription keys must never be forwarded to the DeepSeek API.
    if not key or key.startswith("sk-cp") or not key.isascii() or any(c.isspace() for c in key):
        raise ValueError(
            "Configure DEEPSEEK_API_KEY or private/config/deepseek.local.json (api_key)"
        )
    return DeepSeekExtractionAgent(key)
