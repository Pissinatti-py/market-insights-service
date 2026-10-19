"""Read-only, repeated local ranking benchmark against human review verdicts.

Freeze inputs with --snapshot /tmp/ranking-inputs.json; optionally save vectors
with --cache /tmp/ranking-vectors.json. These files contain source text and must
not be committed. Reported metrics are exploratory, not an independent test.
"""

import argparse
import hashlib
import json
import math
import random
import statistics
import sys
import time
from pathlib import Path

import httpx
from sqlalchemy import select

from src.core.conf import settings
from src.db.session import SyncSession
from src.models.curation import Curation, CurationStatus
from src.models.preference import SINGLETON_ID, Preference
from src.services import ranker
from src.tasks.curation_tasks import _RENDERERS, _embed_text


def load_dataset() -> dict:
    """Read a stable snapshot of source inputs and human labels without changing the database."""
    with SyncSession() as session:
        preference = session.get(Preference, SINGLETON_ID)
        if preference is None:
            raise ValueError("Configure a profile before evaluating the ranker")
        profile = {key: getattr(preference, key) for key in ("stacks", "areas", "keywords", "monitored_libraries")}
        rows = session.scalars(
            select(Curation)
            .where(Curation.status.in_([CurationStatus.APPROVED, CurationStatus.REJECTED]))
            .order_by(Curation.id)
        ).all()
        samples = []
        for row in rows:
            model, _ = _RENDERERS[row.item_type]
            item = session.get(model, row.item_id)
            if item is not None and getattr(item, "deleted_at", None) is None:
                samples.append(
                    {
                        "id": str(row.item_id),
                        "text": _embed_text(row.item_type, item),
                        "approved": row.status == CurationStatus.APPROVED,
                    }
                )
    return {"profile": profile, "samples": samples}


def metrics(scores: list[float], labels: list[bool]) -> dict:
    """Measure discrimination, squared error, and precision at the top of the feed."""
    positives = [score for score, label in zip(scores, labels, strict=True) if label]
    negatives = [score for score, label in zip(scores, labels, strict=True) if not label]
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    return {
        "auc": sum((p > n) + 0.5 * (p == n) for p in positives for n in negatives) / (len(positives) * len(negatives)),
        "brier": statistics.mean((score - label) ** 2 for score, label in zip(scores, labels, strict=True)),
        "precision_at_10": statistics.mean(labels[i] for i in order[:10]),
        "balanced_accuracy_at_0_5": (
            statistics.mean(p >= 0.5 for p in positives) + statistics.mean(n < 0.5 for n in negatives)
        )
        / 2,
        "distinct_scores_at_db_precision": len({round(score, 3) for score in scores}),
    }


def auc_delta_interval(candidate: list[float], baseline: list[float], labels: list[bool]) -> list[float]:
    """Paired stratified bootstrap of fixed predictions; excludes training uncertainty."""
    rng = random.Random(20261019)
    groups = [[i for i, label in enumerate(labels) if label == cls] for cls in (True, False)]
    deltas = []
    for _ in range(1000):
        pos, neg = [rng.choices(group, k=len(group)) for group in groups]
        delta = sum(
            (candidate[p] > candidate[n])
            + 0.5 * (candidate[p] == candidate[n])
            - (baseline[p] > baseline[n])
            - 0.5 * (baseline[p] == baseline[n])
            for p in pos
            for n in neg
        ) / (len(pos) * len(neg))
        deltas.append(delta)
    deltas.sort()
    return [deltas[25], deltas[974]]


def cross_validate(vectors: list[list[float]], samples: list[dict]) -> dict:
    """Repeat five-fold validation ten times, grouping identical source inputs."""
    profile, *items = vectors
    labels = [sample["approved"] for sample in samples]
    groups = {}
    for index, sample in enumerate(samples):
        groups.setdefault(sample["text"], []).append(index)
    if any(len({labels[i] for i in group}) > 1 for group in groups.values()):
        raise ValueError("Identical sources have conflicting review labels; adjudicate before comparing models")
    examples = [(sample["id"], vector, sample["approved"]) for sample, vector in zip(samples, items, strict=True)]
    runs = []
    for seed in range(10):
        folds = [[] for _ in range(5)]
        rng = random.Random(seed)
        for cls in (True, False):
            grouped = [indices for indices in groups.values() if labels[indices[0]] == cls]
            rng.shuffle(grouped)
            for index, indices in enumerate(grouped):
                folds[index % 5].extend(indices)
        scores = [0.0] * len(samples)
        for held in folds:
            train = [example for index, example in enumerate(examples) if index not in held]
            for index in held:
                scores[index] = ranker.score(items[index], train, profile, samples[index]["id"])
        runs.append(metrics(scores, labels))
    return {
        "mean_auc": statistics.mean(row["auc"] for row in runs),
        "mean_brier": statistics.mean(row["brier"] for row in runs),
        "mean_precision_at_10": statistics.mean(row["precision_at_10"] for row in runs),
        "runs": runs,
    }


def input_prefix(model: str) -> str:
    """Use the model's classification task prefix for symmetric neighbour matching."""
    if model.startswith("nomic-embed-text"):
        return "classification: "
    if model.startswith("embeddinggemma"):
        return "task: classification | query: "
    return ""


def predict(vectors: list[list[float]], samples: list[dict], method: str = "knn") -> list[float]:
    """Leave out the target before fitting or checking class support."""
    profile, *items = vectors
    examples = [(sample["id"], vector, sample["approved"]) for sample, vector in zip(samples, items, strict=True)]
    scores = []
    for item_id, vector, _ in examples:
        train = [example for example in examples if example[0] != item_id]
        if method == "centroid":
            centers = [
                [statistics.mean(column) for column in zip(*(v for _, v, label in train if label == cls), strict=True)]
                for cls in (True, False)
            ]
            sims = [math.sumprod(vector, center) / math.sqrt(math.sumprod(center, center)) for center in centers]
            scores.append(1 / (1 + math.exp(-10 * (sims[0] - sims[1]))))
        else:
            scores.append(ranker.score(vector, train, profile, item_id))
    return scores


def evaluate(models: list[str], repeats: int = 3, snapshot: Path | None = None, cache: Path | None = None) -> dict:
    """Compare fixed candidates using identical inputs and separately timed warm calls.

    :param models: Installed Ollama embedding model tags.
    :param repeats: Warm full-corpus passes per model.
    :param snapshot: Reuse this frozen dataset, or create it on the first run.
    :param cache: Optional local output containing all vectors and frozen inputs.
    :return: Metrics, model digests, repeated timings, and paired uncertainty.
    """
    dataset = json.loads(snapshot.read_text()) if snapshot and snapshot.exists() else load_dataset()
    if snapshot and not snapshot.exists():
        snapshot.write_text(json.dumps(dataset, ensure_ascii=False))
    samples = dataset["samples"]
    labels = [sample["approved"] for sample in samples]
    if min(sum(labels), len(labels) - sum(labels)) < 3:
        raise ValueError("At least three reviews per class are needed")
    fingerprint = hashlib.sha256(json.dumps(dataset, sort_keys=True).encode()).hexdigest()
    report = {
        "reviewed": len(samples),
        "approved": sum(labels),
        "dataset_sha256": fingerprint,
        "embed_batch_size": ranker.EMBED_BATCH,
        "repeats": repeats,
        "models": {},
    }
    cached = {"dataset": dataset, "models": {}}
    predictions = {}
    with httpx.Client(base_url=settings.OLLAMA_BASE_URL, timeout=180) as client:
        tags = {row["name"]: row for row in client.get("/api/tags").json()["models"]}
        report["ollama_version"] = client.get("/api/version").json()["version"]
        for model in models:
            prefix = input_prefix(model)
            inputs = [
                prefix + json.dumps(dataset["profile"], ensure_ascii=False),
                *[prefix + s["text"] for s in samples],
            ]
            start = time.perf_counter()
            response = client.post("/api/embed", json={"model": model, "input": [inputs[0]], "keep_alive": "10m"})
            response.raise_for_status()
            load_seconds = time.perf_counter() - start
            times, passes = [], []
            for _ in range(repeats):
                vectors = []
                start = time.perf_counter()
                for offset in range(0, len(inputs), ranker.EMBED_BATCH):
                    response = client.post(
                        "/api/embed",
                        json={
                            "model": model,
                            "input": inputs[offset : offset + ranker.EMBED_BATCH],
                            "keep_alive": "10m",
                        },
                    )
                    response.raise_for_status()
                    vectors.extend(response.json()["embeddings"])
                times.append(time.perf_counter() - start)
                passes.append(vectors)
            tag = tags.get(model, tags.get(model + ":latest", {}))
            model_report = {
                "digest": tag.get("digest"),
                "weight_bytes": tag.get("size"),
                "prefix": prefix,
                "warmup_seconds": load_seconds,
                "warm_corpus_seconds": times,
                "warm_median_seconds": statistics.median(times),
                "variants": {},
            }
            # The same full embeddings isolate the effect of supported Matryoshka truncation.
            for dimensions in [1024, 512, 256] if model.startswith("qwen3-embedding") else [len(passes[0][0])]:
                prepared = []
                for vectors in passes:
                    shortened = [v[:dimensions] for v in vectors]
                    norms = [math.sqrt(math.sumprod(v, v)) for v in shortened]
                    prepared.append([[x / norm for x in v] for v, norm in zip(shortened, norms, strict=True)])
                for method in ("knn", "centroid"):
                    key = f"{dimensions}/{method}"
                    start = time.perf_counter()
                    scores = [predict(vectors, samples, method) for vectors in prepared]
                    scoring_seconds = (time.perf_counter() - start) / repeats
                    model_report["variants"][key] = {
                        "runs": [metrics(s, labels) for s in scores],
                        "loo_evaluation_seconds": scoring_seconds,
                        "max_score_drift": max(
                            max(s[i] for s in scores) - min(s[i] for s in scores) for i in range(len(samples))
                        ),
                    }
                    if method == "knn":
                        model_report["variants"][key]["grouped_five_fold"] = cross_validate(prepared[0], samples)
                    predictions[f"{model}/{key}"] = scores[0]
            report["models"][model] = model_report
            cached["models"][model] = passes
            if cache:
                cache.write_text(json.dumps(cached))
            print(
                f"Completed {model}: {statistics.median(times):.3f}s warm corpus",
                file=sys.stderr,
                flush=True,
            )
    baseline = predictions.get("qwen3-embedding:0.6b/1024/knn", next(iter(predictions.values())))
    for model, row in report["models"].items():
        for variant, result in row["variants"].items():
            result["auc_delta_95_interval_vs_qwen1024_knn"] = auc_delta_interval(
                predictions[f"{model}/{variant}"], baseline, labels
            )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=[settings.OLLAMA_EMBED_MODEL])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--cache", type=Path)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    print(json.dumps(evaluate(args.models, args.repeats, args.snapshot, args.cache), indent=2))
