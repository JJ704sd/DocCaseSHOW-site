import json

import deepseek_extraction as adapter
import pytest

from doccase_preprocessing.extraction_agent import AgentServiceError


def test_routes_send_only_their_evidence_to_deepseek(monkeypatch):
    requests = []

    class Connection:
        def __init__(self, host, *, timeout):
            assert host == "api.deepseek.com" and timeout == 7

        def request(self, method, path, raw, headers):
            assert (method, path) == ("POST", "/chat/completions")
            assert headers["Authorization"] == "Bearer test-only-key"
            requests.append(json.loads(raw))

        def getresponse(self):
            return self

        status = 200

        def read(self):
            return json.dumps(
                {
                    "model": "deepseek-flash",
                    "choices": [
                        {"finish_reason": "stop", "message": {"content": "{}"}},
                    ],
                }
            ).encode()

        def close(self):
            pass

    monkeypatch.setattr(adapter.http.client, "HTTPSConnection", Connection)
    agent = adapter.DeepSeekExtractionAgent("test-only-key")
    narrow = {
        "requirement_id": "destination",
        "field_name": "destination",
        "evidence": [
            {"evidence_id": "E1", "read": "Destination: LAX", "representation": "read"},
        ],
        "original": "not sent",
    }
    material = {
        "source_kind": "current_body",
        "requirements": [],
        "evidence": [],
        "read_sha256": "not sent",
    }
    context = {"requirements": [], "sources": [], "first_inquiry": "not sent"}
    assert agent(narrow, timeout_seconds=7) == "{}"
    assert agent.extract_material(material, timeout_seconds=7) == "{}"
    assert agent.extract_group_context(context, timeout_seconds=7) == "{}"
    assert [set(json.loads(body["messages"][1]["content"])) for body in requests] == [
        {"requirement_id", "field_name", "representation", "field_description", "evidence"},
        {"source_kind", "requirements", "evidence"},
        {"requirements", "sources"},
    ]
    assert all(
        body["model"] == "deepseek-flash"
        and body["thinking"] == {"type": "disabled"}
        and body["response_format"] == {"type": "json_object"}
        for body in requests
    )
    assert all("not sent" not in json.dumps(body) for body in requests)
    material_prompt = requests[1]["messages"][0]["content"]
    assert all(
        term in material_prompt
        for term in (
            "cargo_origin",
            "pickup_city",
            "departure_port",
            "Destino",
            "总计标记",
            "Quantity/PCS",
            "小数逗号",
            "多层表头",
            "context_for",
            "weight_table",
        )
    )


def test_package_count_sends_only_relevant_evidence_and_abstains_when_absent(monkeypatch):
    agent = adapter.DeepSeekExtractionAgent("test-only-key")
    seen = []

    def complete(instruction, payload, timeout):
        seen.append((instruction, payload, timeout))
        return "{}"

    monkeypatch.setattr(agent, "_complete", complete)
    request = {
        "field_name": "package_count",
        "requirement_id": "Q1",
        "material_id": "not-for-model",
        "evidence": [
            {
                "evidence_id": "E1",
                "representation": "markdown",
                "markdown": "Unrelated address. The packages have reduced from 5nos to 4nos.",
            }
        ],
    }
    assert agent(request, timeout_seconds=3) == "{}"
    _, payload, timeout = seen[0]
    assert timeout == 3
    assert payload["evidence"] == [
        {"evidence_id": "E1", "text": "The packages have reduced from 5nos to 4nos."}
    ]
    assert "not-for-model" not in json.dumps(payload)
    request["evidence"] = [{"evidence_id": "E2", "representation": "read", "read": "160 CTNS"}]
    assert agent(request, timeout_seconds=3) == {"requirement_id": "Q1", "candidate": None}
    assert len(seen) == 1


def test_object_requests_send_only_declared_fields(monkeypatch):
    agent = adapter.DeepSeekExtractionAgent("test-only-key")
    sent = []

    def complete(instruction, payload, timeout):
        sent.append((instruction, payload, timeout))
        return "{}"

    monkeypatch.setattr(agent, "_complete", complete)
    request = {
        "object_id": "shipment-example",
        "object_kind": "shipment",
        "requirements": [],
        "evidence": [],
        "private_metadata": "not-for-model",
    }
    assert agent.extract_objects(request, timeout_seconds=3) == "{}"
    _, payload, timeout = sent[0]
    assert set(payload) == {"object_id", "object_kind", "requirements", "evidence"}
    assert timeout == 3 and "not-for-model" not in json.dumps(payload)


@pytest.mark.parametrize(
    ("status", "model", "finish", "failure"),
    [
        (401, "deepseek-flash", "stop", "deepseek_auth_failed"),
        (200, "unrelated-model", "stop", "deepseek_unexpected_model"),
        (200, "deepseek-flash", "length", "deepseek_incomplete_response"),
        (None, "deepseek-flash", "stop", None),
    ],
)
def test_provider_failure_preserves_the_adapter_error_contract(
    monkeypatch, status, model, finish, failure
):
    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.status = status

        def request(self, *_args):
            if status is None:
                raise TimeoutError

        def getresponse(self):
            return self

        def read(self):
            return json.dumps(
                {
                    "model": model,
                    "choices": [
                        {"finish_reason": finish, "message": {"content": "{}"}},
                    ],
                }
            ).encode()

        def close(self):
            pass

    monkeypatch.setattr(adapter.http.client, "HTTPSConnection", Connection)
    agent = adapter.DeepSeekExtractionAgent("test-only-key")
    with pytest.raises(AgentServiceError if failure else TimeoutError, match=failure):
        agent.extract_group_context({"requirements": [], "sources": []}, timeout_seconds=7)


def test_deepseek_credentials_do_not_fall_back_to_minimax(tmp_path, monkeypatch):
    key_file = tmp_path / "deepseek.local.json"
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("MINIMAX_API_KEY", "sk-cp-old")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        adapter.load_deepseek_agent(key_file)
    key_file.write_text('{"api_key":"from-file"}', encoding="utf-8")
    assert adapter.load_deepseek_agent(key_file)._api_key == "from-file"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    assert adapter.load_deepseek_agent(key_file)._api_key == "from-env"
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-cp-old")
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        adapter.load_deepseek_agent(key_file)
