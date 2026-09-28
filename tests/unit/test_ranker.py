"""Importance ranker: kNN over reviewed items, self-exclusion, cold start, embed errors."""

import json
import uuid

import httpx
import pytest
import respx

from src.core.conf import settings
from src.core.exceptions import CollectorRetriable
from src.services import ranker

_EMBED = "http://host.docker.internal:11434/api/embed"

# Unit vectors: "python-ish" items point along x, "crypto-ish" along y.
_X = [1.0, 0.0]
_Y = [0.0, 1.0]
_PROFILE = [0.6, 0.8]


def _examples(approved_vecs, rejected_vecs):
    return [(uuid.uuid4(), v, True) for v in approved_vecs] + [(uuid.uuid4(), v, False) for v in rejected_vecs]


def test_item_near_approved_scores_high_and_near_rejected_low():
    examples = _examples([_X, _X], [_Y, _Y])
    assert ranker.score(_X, examples, _PROFILE, uuid.uuid4()) == 1.0
    assert ranker.score(_Y, examples, _PROFILE, uuid.uuid4()) == 0.0


def test_score_is_similarity_weighted_share_of_approved_neighbours():
    examples = _examples([_X, [0.8, 0.6]], [_Y, [0.6, 0.8]])
    # Similarities to _X: 1.0 and 0.8 approved, 0.0 and 0.6 rejected → 1.8 / 2.4.
    assert ranker.score(_X, examples, _PROFILE, uuid.uuid4()) == pytest.approx(0.75)


def test_only_the_k_nearest_neighbours_vote(monkeypatch):
    monkeypatch.setattr(ranker, "_K", 2)
    far_approved = [[0.1, 0.995]] * 5  # many approvals, but all far from the item
    examples = _examples([_X, _X, *far_approved], [_Y, _Y])
    assert ranker.score(_Y, examples, _PROFILE, uuid.uuid4()) == 0.0


def test_item_is_never_scored_against_its_own_verdict():
    """Otherwise every reviewed item ranks itself and the calibration report lies."""
    target = uuid.uuid4()
    examples = [(target, _X, True)] + _examples([_Y, _Y], [_X, _X])
    # With its own approval it would pull toward 1; left out, its X-neighbours are rejections.
    assert ranker.score(_X, examples, _PROFILE, target) == 0.0


@pytest.mark.parametrize("approved,rejected", [(2, 1), (1, 2), (5, 0), (0, 0)])
def test_one_sided_reviews_fall_back_to_profile_similarity(approved, rejected):
    """No contrast to learn from — a wall of approvals would otherwise push every score up."""
    examples = _examples([_X] * approved, [_Y] * rejected)
    assert ranker.score(_X, examples, _PROFILE, uuid.uuid4()) == pytest.approx(0.6)


def test_profile_fallback_is_clamped_to_zero_one():
    assert ranker.score([-1.0, 0.0], [], _PROFILE, uuid.uuid4()) == 0.0


def test_both_sides_gate_is_judged_on_the_whole_reference_set():
    """At exactly the minimum, a reviewed item must not drop to the fallback scale the others don't use."""
    target = uuid.uuid4()
    examples = [(target, _X, True)] + _examples([_X], [_Y, _Y])
    # Its own verdict is still left out: the one other approval is its only positive neighbour.
    assert ranker.score(_X, examples, _PROFILE, target) == 1.0


def test_vectors_from_different_models_are_rejected_not_truncated():
    examples = _examples([_X, _X], [_Y, _Y])
    with pytest.raises(ValueError):
        ranker.score([1.0, 0.0, 0.0], examples, _PROFILE, uuid.uuid4())


@respx.mock
def test_embed_posts_texts_to_the_embedding_model():
    route = respx.post(_EMBED).mock(return_value=httpx.Response(200, json={"embeddings": [_X, _Y]}))
    assert ranker.embed(["a", "b"]) == [_X, _Y]
    assert json.loads(route.calls.last.request.content) == {"model": settings.OLLAMA_EMBED_MODEL, "input": ["a", "b"]}


@respx.mock
def test_embed_splits_large_inputs_into_batches(monkeypatch):
    monkeypatch.setattr(ranker, "EMBED_BATCH", 2)
    route = respx.post(_EMBED).mock(
        side_effect=lambda request: httpx.Response(
            200, json={"embeddings": [[float(t)] for t in json.loads(request.content)["input"]]}
        )
    )
    assert ranker.embed(["1", "2", "3"]) == [[1.0], [2.0], [3.0]]
    assert [json.loads(call.request.content)["input"] for call in route.calls] == [["1", "2"], ["3"]]


@respx.mock
@pytest.mark.parametrize(
    "response",
    [httpx.ConnectError("refused"), httpx.Response(500), httpx.Response(404, json={"error": "model not found"})],
    ids=["down", "5xx", "model-missing"],
)
def test_embed_failures_are_retriable(response):
    if isinstance(response, Exception):
        respx.post(_EMBED).mock(side_effect=response)
    else:
        respx.post(_EMBED).mock(return_value=response)
    with pytest.raises(CollectorRetriable):
        ranker.embed(["a"])


@respx.mock
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"error": "unexpected"}),
        httpx.Response(200, text="<html>proxy</html>"),
        httpx.Response(200, json={"embeddings": [_X]}),  # one vector for two texts
    ],
    ids=["no-embeddings-key", "not-json", "short"],
)
def test_malformed_embed_responses_are_retriable(response):
    respx.post(_EMBED).mock(return_value=response)
    with pytest.raises(CollectorRetriable):
        ranker.embed(["a", "b"])
