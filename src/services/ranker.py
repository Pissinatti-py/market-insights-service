"""
Importance ranking — a local decision model learned from review verdicts.

The LLM only describes an item (summary/tags); this module decides how high it
ranks. Each item is embedded with a local Ollama embedding model (``/api/embed``
returns unit-length vectors, so cosine is a plain dot product) and scored by its
nearest **reviewed** neighbours: the similarity-weighted share of the ``_K``
closest approved/rejected items that were approved. Measured leave-one-out on
the first 81 real reviews it separates approved from rejected far better than
the LLM's own score did (AUC 0.70 vs 0.41), at a fraction of the cost — and
re-ranking after new reviews needs no LLM call at all (``rerank_all``).

Two invariants carried over from the old few-shot feedback loop:

- **Both sides or nothing.** Under ``_MIN_PER_SIDE`` approved *or* rejected
  examples there is no contrast to learn from (a wall of approvals would just
  push every score up), so the score falls back to similarity with the profile.
  Judged on the whole reference set, not per item, so one run never mixes the
  two scales.
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
    for start in range(0, len(texts), EMBED_BATCH):
        vectors += _embed_batch(texts[start : start + EMBED_BATCH])
    return vectors


def _embed_batch(texts: list[str]) -> list[list[float]]:
    payload = {"model": settings.OLLAMA_EMBED_MODEL, "input": texts}
    try:
        with httpx.Client(timeout=settings.OLLAMA_REQUEST_TIMEOUT_SECONDS) as client:
            resp = client.post(f"{settings.OLLAMA_BASE_URL}/api/embed", json=payload)
            resp.raise_for_status()
            vectors = resp.json()["embeddings"]
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError, KeyError, TypeError) as exc:
        raise CollectorRetriable(f"ollama embed: {type(exc).__name__}: {exc}") from exc
    if len(vectors) != len(texts):
        raise CollectorRetriable(f"ollama embed: {len(vectors)} embeddings for {len(texts)} texts")
    return vectors


def _dot(a: list[float], b: list[float]) -> float:
    return math.sumprod(a, b)  # raises on mismatched lengths — vectors from two different models


def score(vec: list[float], examples: list[Example], profile_vec: list[float], item_id: uuid.UUID) -> float:
    """
    Importance of one item, 0–1: how much its nearest reviewed neighbours were approved.

    :param vec: The item's unit embedding.
    :type vec: list[float]
    :param examples: Every reviewed item with an embedding.
    :type examples: list[Example]
    :param profile_vec: Unit embedding of the user profile — the cold-start fallback.
    :type profile_vec: list[float]
    :param item_id: The item being scored; its own review is left out.
    :type item_id: uuid.UUID
    :return: Similarity-weighted share of approved neighbours, or the clamped profile
        similarity while either side has fewer than ``_MIN_PER_SIDE`` examples.
    :rtype: float
    """
    approvals = sum(approved for _, _, approved in examples)
    if min(approvals, len(examples) - approvals) < _MIN_PER_SIDE:
        return min(max(_dot(vec, profile_vec), 0.0), 1.0)

    pool = [(example_vec, approved) for example_id, example_vec, approved in examples if example_id != item_id]

    # ponytail: pure-Python O(items × reviews) dot products; numpy/pgvector once reviews reach thousands
    nearest = heapq.nlargest(_K, ((_dot(vec, v), approved) for v, approved in pool), key=operator.itemgetter(0))
    weight = sum(max(sim, 0.0) for sim, _ in nearest)
    if not weight:  # every neighbour points away — fall back to a plain vote
        return sum(approved for _, approved in nearest) / len(nearest)
    return sum(max(sim, 0.0) for sim, approved in nearest if approved) / weight
