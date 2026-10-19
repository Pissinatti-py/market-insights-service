# Curation evaluation — 2026-10-19

> Historical first experiment. See [the follow-up model review](curation-model-review.md) for the current prompt, repeated validation, and model-selection decision.

The maximum scores came from the ranking algorithm, not the summary prompt.
In the initial snapshot, 147 of 7,453 curations had exactly `1.000`, including
123 articles. An off-topic nail-art article had ten approved nearest neighbours,
all with cosine similarities between approximately 0.47 and 0.53. Dividing only
positive votes by all votes produced `1.000` despite the weak absolute evidence.
The two-decimal UI also hid differences at the database's three-decimal precision.

## Ranking experiment

82 human reviews (52 approved, 30 rejected) were evaluated leave-one-out: each
item's own verdict was excluded. The source rendering, labels, and profile were
held constant when comparing embedding models. Both models use 1,024 dimensions;
their vectors are nevertheless incompatible.

The first comparison used 16 texts per embedding request.

| Embedding and ranking | AUC ↑ | Brier error ↓ |
|---|---:|---:|
| `bge-m3`, original weighted vote | 0.694 | 0.206 |
| `bge-m3`, neutral-prior smoothing | 0.692 | 0.206 |
| `qwen3-embedding:0.6b`, original vote | 0.712 | 0.200 |
| `qwen3-embedding:0.6b`, neutral-prior smoothing | 0.711 | 0.201 |

A [repeat with production-size batches of 32 and stable ID ordering](benchmarks/ranking-20261019.json)
measured AUC 0.733 and Brier error 0.195 for smoothed Qwen, versus 0.692 and
0.206 for smoothed BGE. Variation across runs with different batching/order is
another reason to treat the model comparison as provisional.

The selected ranking uses `(positive_similarity + 0.5) / (total_similarity + 1)`.
This Beta(0.5, 0.5) prior introduces uncertainty without clipping or adding random
jitter. Unanimity is differentiated by evidence strength; no positive similarity
returns a neutral 0.5. The cold-start path still uses profile similarity when
either reviewed class has fewer than two examples.

The nail-art probe moved from 1.000 to approximately 0.583 with the selected model
and smoothing. It is still not a reliable low-relevance prediction: the training
set needs representative off-topic rejections. A high score estimates affinity to
past editorial decisions; it does not establish technical quality or novelty.
The small review set was used to choose a candidate, so these figures are
exploratory, not an independent test or a calibrated accuracy guarantee.
Other explored approaches included a regularized logistic classifier and
class-balanced neighbour voting; neither clearly improved both discrimination
and error on this sample.

The installed Qwen embedding weights occupy about 639 MB versus 1,158 MB for
`bge-m3`. Warm batch timings did not establish a speed advantage for Qwen. The
choice is based on the observed ranking quality and smaller model footprint.

`embedding_model` now records provenance. `rerank_all` replaces missing,
untagged, or different-model vectors, and training/scoring filters on that tag.
It preserves summaries, tags, human review decisions, and review timestamps.
If a tag is repointed to different weights, clear its provenance before reranking.

The applied rebuild embedded and ranked 7,650 curations, changing 7,625 scores.
At the subsequent check, all 7,690 curations (including new arrivals) had the new
embedding-model tag and none had `1.000`. The nail-art probe scored 0.582, the
mini-PC advertisement 0.747, and the Rust-allocation article 0.845. The mini-PC
score is still too generous for the intended technical focus; removing maximum
score saturation does not solve every editorial-relevance error.

Reproduce without changing application data:

```bash
docker compose exec -T api python -m scripts.evaluate_ranker \
  --models bge-m3 qwen3-embedding:0.6b
```

The report includes a dataset fingerprint. New reviews or source changes can
change the result. This command calls the embedding server and can contend with
an active curation job.

## Summary model experiment

The active summary model was `qwen3:14b` Q4_K_M, although the previous defaults
documented Q8. Six synthetic probes covered an unrelated lifestyle article, a
title-only Rust article, a **fictional** middleware security advisory, unsupported
marketing, instructions embedded in an article, and missing source content.
The fixed probe profile is in `scripts/benchmark_curation.py`.

An initial run suffered model reloads while the worker was curating. The table
below uses a second run with the worker stopped, the refined prompt, 8,192-token
context, thinking disabled, and JSON-schema output. The first request for each
model is excluded from the warm median. Each model was tested once per probe;
these are local observations, not general model benchmarks.

| Installed model | Weight size | Warm median | Generation tokens/s | Valid JSON |
|---|---:|---:|---:|---:|
| `qwen3:14b` Q4_K_M | 9.28 GB | 1.263 s | 61.1 | 6/6 |
| `qwen3.5:9b` Q4_K_M | 6.59 GB | 1.339 s | 92.3 | 6/6 |
| `qwen3.6:35b-a3b-q4_K_M` | 23.94 GB | 2.003 s | 57.7 | 6/6 |

Keep `qwen3:14b` for summaries. The 9B model generated tokens faster but produced
longer answers, no lower median request latency, and an incorrect claim that the
FastAPI tutorial was unrelated to a profile explicitly containing FastAPI. The
35B model used substantially more memory and had a higher observed latency
without a decisive quality improvement. All three still made questionable
profile-fit statements in some cases; valid JSON does not establish correctness.

The revised prompt separates profile fit, novelty, substance, and evidence;
distinguishes missing evidence from irrelevance; avoids equating popularity,
version numbers, or new publication dates with importance; and treats fetched
content as untrusted data. Summary generation remains separate from numeric
ranking. Existing summaries are preserved during reranking; future curations
use the revised prompt. Output is bounded to 256 tokens in the application.

[Raw results](benchmarks/curation-models-20261019.jsonl) used an output limit of
300 tokens; no response approached either limit. Reproduce with:

```bash
docker compose exec -T api python -m scripts.benchmark_curation \
  --models qwen3:14b qwen3.5:9b qwen3.6:35b-a3b-q4_K_M
```

## Primary references

- [TypeSafe confidence](https://docs.typesafe.ai/confidence): uncertainty is distinct
  from a guaranteed correct decision; the local affinity score is not Jev confidence.
- [Qwen3 embedding model card](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B).
- [Ollama structured outputs](https://docs.ollama.com/capabilities/structured-outputs):
  constraining the response format still requires checking the content.
- [Ollama thinking controls](https://docs.ollama.com/capabilities/thinking).
