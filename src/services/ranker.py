"""
Importance ranking — a local decision model learned from review verdicts.

The LLM only describes an item (summary/tags); this module decides how high it
ranks. Each item is embedded with a local Ollama embedding model (``/api/embed``
returns unit-length vectors, so cosine is a plain dot product) and scored by its
nearest **reviewed** neighbours: a smoothed, similarity-weighted approval estimate
over the ``_K`` closest approved/rejected items. A neutral Beta(0.5, 0.5) prior
prevents a handful of unanimous, weakly similar reviews from implying certainty.
Re-ranking after new reviews needs no generation call (``rerank_all``).
Model comparisons and their dataset-dependent limitations are documented in
``docs/curation-model-review.md``.

Two invariants carried over from the old few-shot feedback loop:

- **Both sides or nothing.** Under ``_MIN_PER_SIDE`` approved *or* rejected
  examples there is no contrast to learn from (a wall of approvals would just
  push every score up), so the score falls back to similarity with the profile.
  The target's own label is excluded before checking class support as well as
  before voting, including at the cold-start boundary.
- **Never its own verdict.** An item is never scored against its own review —
  otherwise every reviewed item would rank itself and
  ``GET /api/curation/calibration`` would report a self-fulfilling separation.
"""

from __future__ import annotations

import heapq
import math
import operator
import uuid

import httpx

from src.core.conf import settings
from src.core.exceptions import CollectorRetriable

#: Neighbours consulted per item — 10–15 scored best leave-one-out on the first reviews.
_K = 10

#: Minimum reviewed examples on *each* side before the neighbours are trusted.
_MIN_PER_SIDE = 2

#: One neutral observation (half approved, half rejected), in similarity-weight units.
_PRIOR_WEIGHT = 1.0

#: Characters of the rendered item that get embedded — the head carries title and lead.
EMBED_CHARS = 2000

#: Texts per /api/embed request — keeps each call well under the request timeout.
EMBED_BATCH = 32

#: One reviewed item: ``(item_id, unit embedding, approved?)``.
Example = tuple[uuid.UUID, list[float], bool]


def embed(texts: list[str]) -> list[list[float]]:
    """
    Embed ``texts`` with the local embedding model, one unit vector per text.

    Sent in requests of ``EMBED_BATCH`` texts.

    :param texts: Texts to embed, in order.
    :type texts: list[str]
    :return: One embedding per input text.
    :rtype: list[list[float]]
    :raises CollectorRetriable: Ollama unreachable, timeout, an error status, or a
        body without one embedding per text.
    """
    vectors: list[list[float]] = []
    if not texts:
        return vectors
    with httpx.Client(timeout=settings.OLLAMA_REQUEST_TIMEOUT_SECONDS) as client:
        for start in range(0, len(texts), EMBED_BATCH):
            batch = _embed_batch(texts[start : start + EMBED_BATCH], client)
            if vectors and len(batch[0]) != len(vectors[0]):
                raise CollectorRetriable("ollama embed: vector dimensions changed between batches")
            vectors.extend(batch)
    return vectors


def _embed_batch(texts: list[str], client: httpx.Client) -> list[list[float]]:
    payload = {"model": settings.OLLAMA_EMBED_MODEL, "input": texts}
    try:
        resp = client.post(f"{settings.OLLAMA_BASE_URL}/api/embed", json=payload)
        resp.raise_for_status()
        vectors = resp.json()["embeddings"]
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError, KeyError, TypeError) as exc:
        raise CollectorRetriable(f"ollama embed: {type(exc).__name__}: {exc}") from exc
    if not isinstance(vectors, list) or len(vectors) != len(texts):
        raise CollectorRetriable("ollama embed: expected one vector per input text")
    normalized = []
    dimensions = None
    for vector in vectors:
        if not isinstance(vector, list) or not vector:
            raise CollectorRetriable("ollama embed: expected a non-empty numeric vector")
        if dimensions is not None and len(vector) != dimensions:
            raise CollectorRetriable("ollama embed: inconsistent vector dimensions")
        dimensions = len(vector)
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in vector):
            raise CollectorRetriable("ollama embed: vector contains non-finite or non-numeric values")
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0:
            raise CollectorRetriable("ollama embed: vector has an invalid norm")
        normalized.append([value / norm for value in vector])
    return normalized


def _dot(a: list[float], b: list[float]) -> float:
    return math.sumprod(a, b)  # raises on mismatched lengths — vectors from two different models


def score(vec: list[float], examples: list[Example], profile_vec: list[float], item_id: uuid.UUID) -> float:
    """
    Estimate affinity from reviewed neighbours, with uncertainty shrinkage.

    :param vec: The item's unit embedding.
    :type vec: list[float]
    :param examples: Every reviewed item with an embedding.
    :type examples: list[Example]
    :param profile_vec: Unit embedding of the user profile — the cold-start fallback.
    :type profile_vec: list[float]
    :param item_id: The item being scored; its own review is left out.
    :type item_id: uuid.UUID
    :return: Smoothed affinity estimate, or the clamped profile similarity while
        either side has fewer than ``_MIN_PER_SIDE`` examples. This is a ranking
        signal, not a calibrated probability of editorial importance.
    :rtype: float
    """
    pool = [(example_vec, approved) for example_id, example_vec, approved in examples if example_id != item_id]
    approvals = sum(approved for _, approved in pool)
    if min(approvals, len(pool) - approvals) < _MIN_PER_SIDE:
        return min(max(_dot(vec, profile_vec), 0.0), 1.0)

    # ponytail: pure-Python O(items × reviews) dot products; numpy/pgvector once reviews reach thousands
    nearest = heapq.nlargest(_K, ((_dot(vec, v), approved) for v, approved in pool), key=operator.itemgetter(0))
    weight = sum(max(sim, 0.0) for sim, _ in nearest)
    positive = sum(max(sim, 0.0) for sim, approved in nearest if approved)
    # Fractional observations: closer evidence moves the estimate further from
    # neutral. No positive similarity means no evidence, regardless of the labels.
    return (positive + _PRIOR_WEIGHT / 2) / (weight + _PRIOR_WEIGHT)
