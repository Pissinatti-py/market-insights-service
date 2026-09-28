import json

import httpx
import pytest
import respx

from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.schemas.curation_schema import CurationCreate
from src.services.agents import curation_agent

_OLLAMA = "http://host.docker.internal:11434/api/chat"


def _ollama_response(content: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"content": content}})


@respx.mock
def test_curate_validates_good_output():
    body = '{"summary": "A fast Rust web framework", "tags": ["Rust", "rust", "web"]}'
    respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    result, raw = curation_agent.curate("repo: axum", {"stacks": ["rust"]})
    assert result.summary.startswith("A fast")
    assert result.tags == ["rust", "web"]  # deduped + lowercased
    assert raw["summary"] == "A fast Rust web framework"


@respx.mock
def test_curate_asks_for_a_description_only_without_thinking():
    """The LLM describes; the ranker scores. Thinking was ~10x the latency of the answer."""
    body = '{"summary": "ok", "tags": []}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {})
    sent = json.loads(route.calls.last.request.content)
    assert sent["format"] == CurationCreate.model_json_schema()
    assert set(sent["format"]["properties"]) == {"summary", "tags"}
    assert sent["think"] is False


@respx.mock
def test_curate_includes_context_in_prompt():
    body = '{"summary": "ok", "tags": []}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {}, context="FULL ARTICLE CONTENT:\nhello world")
    sent = json.loads(route.calls.last.request.content)
    user_msg = sent["messages"][-1]["content"]
    assert "EXTRA CONTEXT:" in user_msg
    assert "hello world" in user_msg


@respx.mock
def test_curate_omits_context_block_when_empty():
    body = '{"summary": "ok", "tags": []}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {})
    sent = json.loads(route.calls.last.request.content)
    assert "EXTRA CONTEXT:" not in sent["messages"][-1]["content"]


@respx.mock
def test_user_prompt_carries_today_and_full_profile():
    """TODAY lets the summary flag old news; monitored_libraries must reach the model."""
    body = '{"summary": "ok", "tags": []}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {"monitored_libraries": ["pypi:fastapi"]})
    user_msg = json.loads(route.calls.last.request.content)["messages"][1]["content"]
    assert "TODAY: " in user_msg
    assert "pypi:fastapi" in user_msg


@respx.mock
def test_curate_rejects_missing_summary():
    respx.post(_OLLAMA).mock(return_value=_ollama_response('{"tags": ["x"]}'))
    with pytest.raises(CollectorTerminal):
        curation_agent.curate("item", {})


@respx.mock
def test_curate_rejects_non_json():
    respx.post(_OLLAMA).mock(return_value=_ollama_response("not json at all"))
    with pytest.raises(CollectorTerminal):
        curation_agent.curate("item", {})


@respx.mock
def test_curate_unavailable_is_retriable():
    respx.post(_OLLAMA).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(CollectorRetriable):
        curation_agent.curate("item", {})
