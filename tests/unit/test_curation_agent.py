import json
import math

import httpx
import pytest
import respx

from src.core.exceptions import CollectorRetriable, CollectorTerminal
from src.services.agents import curation_agent

_OLLAMA = "http://host.docker.internal:11434/api/chat"


def _ollama_response(content: str, logprobs: list[dict] | None = None) -> httpx.Response:
    payload: dict = {"message": {"content": content}}
    if logprobs is not None:
        payload["logprobs"] = logprobs
    return httpx.Response(200, json=payload)


def _band_logprobs(alternatives: dict[str, float]) -> list[dict]:
    """Tokens for `{"band": "<pick>"}` where the band position offers ``alternatives`` (token → probability)."""
    picked = max(alternatives, key=alternatives.get)
    top = [{"token": token, "logprob": math.log(p)} for token, p in alternatives.items()]
    return [
        {"token": '{"', "logprob": 0.0, "top_logprobs": []},
        {"token": "band", "logprob": 0.0, "top_logprobs": []},
        {"token": '":', "logprob": 0.0, "top_logprobs": []},
        {"token": ' "', "logprob": 0.0, "top_logprobs": []},
        {"token": picked, "logprob": math.log(alternatives[picked]), "top_logprobs": top},
        {"token": '"}', "logprob": 0.0, "top_logprobs": []},
    ]


@respx.mock
def test_curate_validates_good_output():
    body = '{"summary": "A fast Rust web framework", "tags": ["Rust", "rust", "web"], "band": "relevant"}'
    respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    result, raw = curation_agent.curate("repo: axum", {"stacks": ["rust"]})
    assert result.summary.startswith("A fast")
    assert result.tags == ["rust", "web"]  # deduped + lowercased
    assert result.importance_score == 0.8  # the band anchor — no logprobs in this response
    assert raw["band"] == "relevant"
    assert raw["score_source"] == "label"


@respx.mock
def test_curate_sends_decision_schema_and_asks_for_logprobs():
    body = '{"summary": "ok", "tags": [], "band": "routine"}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {})
    sent = json.loads(route.calls.last.request.content)
    assert sent["format"] == curation_agent._Decision.model_json_schema()
    assert sent["format"]["properties"]["band"]["enum"] == ["noise", "routine", "relevant", "must_see"]
    assert list(sent["format"]["properties"])[0] == "band"  # decided before the summary can anchor it
    assert sent["logprobs"] is True
    assert sent["top_logprobs"] >= 4  # all four bands must be visible at the band position


@respx.mock
def test_score_is_band_anchors_weighted_by_probability():
    body = '{"summary": "ok", "tags": [], "band": "routine"}'
    logprobs = _band_logprobs({"routine": 0.6, "relevant": 0.4})
    respx.post(_OLLAMA).mock(return_value=_ollama_response(body, logprobs))
    result, raw = curation_agent.curate("item", {})
    assert result.importance_score == pytest.approx(0.55 * 0.6 + 0.80 * 0.4, abs=1e-3)
    assert raw["score_source"] == "logprobs"
    assert raw["band_probs"] == pytest.approx({"routine": 0.6, "relevant": 0.4})


@respx.mock
def test_score_merges_case_variants_and_ignores_non_band_tokens():
    """top_logprobs come before the grammar mask — `Routine` counts as routine, `D` is dropped."""
    body = '{"summary": "ok", "tags": [], "band": "routine"}'
    logprobs = _band_logprobs({"routine": 0.5, "Routine": 0.2, "D": 0.2, "must": 0.1})
    respx.post(_OLLAMA).mock(return_value=_ollama_response(body, logprobs))
    result, raw = curation_agent.curate("item", {})
    assert raw["band_probs"] == pytest.approx({"routine": 0.875, "must_see": 0.125})
    assert result.importance_score == pytest.approx(0.55 * 0.875 + 0.95 * 0.125, abs=1e-3)


@respx.mock
def test_score_falls_back_to_anchor_when_band_position_is_missing():
    body = '{"summary": "ok", "tags": [], "band": "noise"}'
    logprobs = [{"token": "{}", "logprob": 0.0, "top_logprobs": []}]
    respx.post(_OLLAMA).mock(return_value=_ollama_response(body, logprobs))
    result, raw = curation_agent.curate("item", {})
    assert result.importance_score == 0.2
    assert raw["score_source"] == "label"


@respx.mock
def test_curate_includes_context_in_prompt():
    body = '{"summary": "ok", "tags": [], "band": "routine"}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {}, context="FULL ARTICLE CONTENT:\nhello world")
    sent = json.loads(route.calls.last.request.content)
    user_msg = sent["messages"][-1]["content"]
    assert "EXTRA CONTEXT:" in user_msg
    assert "hello world" in user_msg


@respx.mock
def test_curate_omits_context_block_when_empty():
    body = '{"summary": "ok", "tags": [], "band": "routine"}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {})
    sent = json.loads(route.calls.last.request.content)
    assert "EXTRA CONTEXT:" not in sent["messages"][-1]["content"]


@respx.mock
def test_system_prompt_carries_scoring_rubric():
    """The calibration rubric + anchor examples must reach the model — guards against prompt regressions."""
    body = '{"summary": "ok", "tags": [], "band": "routine"}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {})
    system_msg = json.loads(route.calls.last.request.content)["messages"][0]["content"]
    assert "must_see:" in system_msg and "noise:" in system_msg
    assert "do NOT default to the top band" in system_msg
    assert "Anchor examples" in system_msg
    assert "stale" in system_msg  # recency instruction


@respx.mock
def test_user_prompt_carries_today_and_full_profile():
    """TODAY grounds the staleness cap; monitored_libraries must reach the model."""
    body = '{"summary": "ok", "tags": [], "band": "routine"}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {"monitored_libraries": ["pypi:fastapi"]})
    user_msg = json.loads(route.calls.last.request.content)["messages"][1]["content"]
    assert "TODAY: " in user_msg
    assert "pypi:fastapi" in user_msg


def _feedback(approved: int, rejected: int) -> list[dict]:
    return [
        {
            "decision": decision,
            "item_id": f"{decision}-{i}",
            "summary": f"{decision} item {i}",
            "tags": [decision[:3]],
            "score": 0.5,
        }
        for decision, count in (("approved", approved), ("rejected", rejected))
        for i in range(count)
    ]


@respx.mock
def test_feedback_block_reaches_the_prompt_with_both_sides():
    body = '{"summary": "ok", "tags": [], "band": "routine"}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {}, feedback=_feedback(approved=2, rejected=2))
    user_msg = json.loads(route.calls.last.request.content)["messages"][1]["content"]
    assert "REVIEW FEEDBACK" in user_msg
    assert "APPROVED (kept" in user_msg and "REJECTED (discarded" in user_msg
    assert "approved item 0" in user_msg and "rejected item 1" in user_msg
    # Feedback belongs with the profile, ahead of the item being judged.
    assert user_msg.index("REVIEW FEEDBACK") < user_msg.index("ITEM:")


@respx.mock
@pytest.mark.parametrize(
    "approved,rejected",
    [(2, 1), (1, 2), (5, 0), (0, 0)],
    ids=["too-few-rejected", "too-few-approved", "one-sided", "empty"],
)
def test_feedback_block_dropped_without_contrast_on_both_sides(approved, rejected):
    """One-sided feedback has nothing to contrast against — fall back to the static anchors."""
    body = '{"summary": "ok", "tags": [], "band": "routine"}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {}, feedback=_feedback(approved, rejected))
    assert "REVIEW FEEDBACK" not in json.loads(route.calls.last.request.content)["messages"][1]["content"]


@respx.mock
def test_system_prompt_ranks_feedback_above_anchors():
    """Without this the model treats the fictional anchors as equal to real decisions."""
    body = '{"summary": "ok", "tags": [], "band": "routine"}'
    route = respx.post(_OLLAMA).mock(return_value=_ollama_response(body))
    curation_agent.curate("item", {})
    system_msg = json.loads(route.calls.last.request.content)["messages"][0]["content"]
    assert "REVIEW FEEDBACK" in system_msg
    assert "outranks" in system_msg


@respx.mock
def test_curate_rejects_unknown_band():
    respx.post(_OLLAMA).mock(return_value=_ollama_response('{"summary": "x", "tags": [], "band": "amazing"}'))
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
