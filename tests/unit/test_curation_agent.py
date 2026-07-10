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
    body = '{"summary": "A fast Rust web framework", "tags": ["Rust", "rust", "web"], "importance_score": 0.8}'
    respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    result, raw = curation_agent.curate("repo: axum", {"stacks": ["rust"]})
    assert result.summary.startswith("A fast")
    assert result.tags == ["rust", "web"]  # deduped + lowercased
    assert result.importance_score == 0.8  # float, per the schema (Decimal breaks Ollama's grammar)
    assert raw["importance_score"] == 0.8


@respx.mock
def test_curate_sends_output_schema_as_format():
    body = '{"summary": "ok", "tags": [], "importance_score": 0.5}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {})
    sent = json.loads(route.calls.last.request.content)
    assert sent["format"] == CurationCreate.model_json_schema()


@respx.mock
def test_curate_includes_context_in_prompt():
    body = '{"summary": "ok", "tags": [], "importance_score": 0.5}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {}, context="FULL ARTICLE CONTENT:\nhello world")
    sent = json.loads(route.calls.last.request.content)
    user_msg = sent["messages"][-1]["content"]
    assert "EXTRA CONTEXT:" in user_msg
    assert "hello world" in user_msg


@respx.mock
def test_curate_omits_context_block_when_empty():
    body = '{"summary": "ok", "tags": [], "importance_score": 0.5}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {})
    sent = json.loads(route.calls.last.request.content)
    assert "EXTRA CONTEXT:" not in sent["messages"][-1]["content"]


@respx.mock
def test_system_prompt_carries_scoring_rubric():
    """The calibration rubric must reach the model — guards against prompt regressions."""
    body = '{"summary": "ok", "tags": [], "importance_score": 0.5}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {})
    system_msg = json.loads(route.calls.last.request.content)["messages"][0]["content"]
    assert "0.9-1.0" in system_msg
    assert "do NOT default to the top band" in system_msg


@respx.mock
def test_curate_rejects_out_of_range_score():
    respx.post(_OLLAMA).mock(return_value=_ollama_response('{"summary": "x", "tags": [], "importance_score": 5}'))
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
