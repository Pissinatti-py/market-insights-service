# Local curation model review — 2026-10-19

## Decision

Keep `qwen3:14b` Q4_K_M for summaries/tags and `qwen3-embedding:0.6b`
Q8_0 with 1,024 dimensions for ranking after the initial model migration.
Use the shorter factual description prompt. Faster alternatives did not
consistently improve precision as well as latency.

The prompt improved measured description latency and source fidelity. Numeric
ranking did not demonstrate a further precision improvement in this follow-up;
its embedding model and smoothed kNN rule remain unchanged. This is the safest
choice among the tested configurations, not a claim about every local model.

## Protocol and limits

The curation worker was idle before being stopped for the benchmarks. Calls
used the existing local Ollama 0.32.14 server without paid inference or changes
to articles, descriptions, or human decisions. Warm timings exclude downloads
and initial model loading and apply to this server and these inputs.

- Ranking used a frozen snapshot of 84 reviews: 53 approved and 31 rejected.
  Every model received identical source inputs in stable order, with three
  embedding passes after warmup and batches of 32. The report records model
  digests, task prefixes, dataset fingerprint, and repeated timings.
- Quality checks used leave-one-out and ten seeded repetitions of grouped,
  stratified five-fold validation. Identical texts are grouped together; this
  snapshot contains 84 distinct texts. A target is excluded before checking
  class support as well as before voting.
- Descriptions used 12 synthetic challenge cases, three seeds each, followed
  by eight additional cases for the finalists. Cases include Portuguese, long
  content, historical events, missing evidence, unsupported claims, and malicious
  instructions inside source text. The request builder is shared with production:
  JSON schema, temperature 0.1, context 8192, output limit 256, thinking disabled.
- Tag checks require central source technologies and forbid specified unrelated
  tags. They do not measure comprehensive factual accuracy. Summaries were also
  inspected: the 8B model's invented React detail passed its automatic tag check.

Repeated seeds and folds are not independent examples. The small review set
was already used for model exploration; this is not an untouched production
test. AUC measures agreement with keep/reject decisions, not factual truth or
novelty. The paired bootstrap uses fixed predictions and omits training
uncertainty. Brier error does not make the affinity score a calibrated probability.

## Ranking results

Warm times cover the same 85 inputs: profile plus 84 reviewed sources. Five-fold
metrics average the same ten seeded splits, using the first embedding pass.

| Embedding / smoothed kNN | Warm corpus median | LOO AUC, three runs | Five-fold AUC | Five-fold Brier | Mean P@10 |
|---|---:|---:|---:|---:|---:|
| Qwen3 embedding 0.6B, 1024 | 1.386 s | 0.685 / 0.689 / 0.695 | **0.7140** | **0.2034** | 0.81 |
| BGE-M3, 1024 | 1.124 s | 0.672 / 0.672 / 0.672 | 0.6918 | 0.2092 | **0.86** |
| EmbeddingGemma 300M, 768 | 0.869 s | 0.608 / 0.608 / 0.608 | 0.5940 | 0.2355 | 0.71 |
| Nomic Embed Text v1.5, 768 | **0.378 s** | **0.719 / 0.719 / 0.719** | 0.7091 | 0.2064 | 0.83 |
| Qwen3 embedding 0.6B, truncated to 512 | same encoder pass | 0.699 / 0.699 / 0.698 | 0.7029 | 0.2044 | 0.77 |

Nomic encoded approximately 3.7 times faster. Its LOO advantage did not persist
in five-fold AUC or Brier, although P@10 was slightly higher. The paired 95%
bootstrap interval for its LOO AUC advantage over Qwen was approximately
[-0.051, +0.114]. This establishes neither an accuracy gain nor equivalence.

Truncation to 512 dimensions halved dot-product work but reduced five-fold
quality and P@10. Truncation to 256 also hurt LOO performance. A nearest-class-
centroid rule did not provide enough discrimination improvement to replace kNN.
The report's `loo_evaluation_seconds` includes repeated fitting for centroid
evaluation and must not be interpreted as production inference latency.

Qwen vectors changed slightly across identical requests (minimum repeat cosine
about 0.99938); top-k selection amplified some differences into score changes
around 0.084 at 1,024 dimensions. The cause of this numerical variation has not
been established. Other tested embeddings were identical across these passes.

[Raw ranking results](benchmarks/ranking-review-20261019.json) contain aggregated
metrics. Private source snapshots and vectors remain in local `/tmp` files and
are not committed.

## Summary and tag results

With the previous long prompt:

| Model | Warm median | Tag checks passed |
|---|---:|---:|
| Qwen3 14B | 1.281 s | 33/36 |
| Qwen3.5 4B | 1.583 s | 32/36 |
| Qwen3.5 9B | 1.253 s | 31/36 |
| Qwen3-VL 8B Instruct | **0.755 s** | **35/36** |

With the shorter factual prompt:

| Model | Warm median | Tag checks passed | Content review |
|---|---:|---:|---|
| **Qwen3 14B** | 1.118 s | **36/36** | Preserved hypothetical advisory and affected-component scope |
| Qwen3.5 9B | 1.017 s | 34/36 | Missed required tags and hypothetical qualification |
| Qwen3-VL 8B Instruct | 0.679 s | 33/36 | Copied injected LangChain/Rust tags |
| Qwen3 8B | **0.640 s** | **36/36** | Presented a hypothetical fix as a real event in all three runs |

The additional eight cases were decisive:

| Configuration | Warm median | Tag checks passed | Observed content failures |
|---|---:|---:|---|
| 14B, previous prompt | 1.461 s | 24/24 | Called monitored FastAPI and other technical content unrelated |
| **14B, factual prompt** | 1.127 s | **24/24** | Those errors were absent; preserved the long article's negative benchmark result |
| 8B, factual prompt | **0.670 s** | 21/24 | Followed the Vue source injection and invented a LangChain vulnerability; added unsupported React details |

The 14B prompt change reduced median latency by 12.8% on the initial cases and
22.9% on the additional cases. Initial tag checks improved from 33/36 to 36/36;
the additional set remained 24/24. These observations on 20 synthetic cases do
not guarantee error-free production behavior.

The prompt asks for a concrete finding and supported limitation, without a
second importance/relevance verdict that can contradict the ranker. It retains
source-instruction handling, historical/advertisement distinctions, missing-data
qualifications, and affected-component scope. The 35-word instruction is guidance,
not a hard validated word limit. Existing summaries and human verdicts are
preserved; future curations use the prompt.

Artifacts:

- [All repeated outputs](benchmarks/curation-review-20261019.jsonl)
- [Aggregate timings and checks](benchmarks/curation-review-summary-20261019.json)
- [Exact prompts](benchmarks/curation-prompts-20261019.json)
- [Cases and expected tag checks](../scripts/benchmark_curation.py)

## Review corrections

- Reject empty, nonnumeric, nonfinite, zero-norm, or inconsistent-size embedding
  vectors before persistence; normalize valid vectors defensively.
- Reuse one HTTP client across an embedding operation's batches.
- Exclude the target's own label before checking class support, closing leakage
  at the cold-start boundary. Exactly two reviews in a class can therefore place
  a reviewed target on the fallback scale while unreviewed targets use kNN.
- Load reviews in stable order to resolve equal-similarity ties consistently.
- Treat malformed Ollama response envelopes as retryable transport failures.
- Share production request construction with benchmarks and use import-safe CLIs.
- Correct the enrichment documentation: descriptions use the fetched full body;
  numeric ranking uses the stored source excerpt, up to 2,000 characters.

The provenance migration, model-space filter, neutral prior, and three-decimal
score display remain useful. A mutable tag does not detect changed weights or
preprocessing: replacing weights under the same tag requires invalidating and
rebuilding embeddings. Recorded benchmark digests are not production enforcement.

## Reproduction

Run while the curation worker is idle to avoid competing inference. Snapshot and
cache paths contain private source inputs; these commands do not write application data.

```bash
docker compose exec -T api python -m scripts.evaluate_ranker \
  --models qwen3-embedding:0.6b bge-m3 embeddinggemma:300m nomic-embed-text:v1.5 \
  --repeats 3 --snapshot /tmp/ranking-review-inputs.json \
  --cache /tmp/ranking-review-vectors.json

docker compose exec -T api python -m scripts.benchmark_curation \
  --models qwen3:14b qwen3.5:4b qwen3.5:9b qwen3-vl:8b-instruct --prompt baseline

docker compose exec -T api python -m scripts.benchmark_curation \
  --models qwen3:14b qwen3.5:9b qwen3-vl:8b-instruct qwen3:8b --prompt concise

docker compose exec -T api python -m scripts.benchmark_curation \
  --models qwen3:14b --prompt baseline --suite holdout

docker compose exec -T api python -m scripts.benchmark_curation \
  --models qwen3:14b qwen3:8b --prompt concise --suite holdout
```

## Jev and local decision architectures

The [official Jev documentation](https://docs.typesafe.ai/introduction) describes
typed decisions and distributions; its [model interface](https://docs.typesafe.ai/models)
is an API. No local Jev package or weights were found in the consulted official
material or installed Ollama catalog. Jev was not executed in this review.

[Qwen3 Reranker](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B) is a local
query/document relevance alternative. Its scoring contract differs from the
embedding endpoint and would require another inference path. It was considered
architecturally, not benchmarked or declared inferior.

Primary model references:

- [Qwen3 embeddings and supported dimensions](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B)
- [Nomic task prefixes](https://huggingface.co/nomic-ai/nomic-embed-text-v1.5)
- [EmbeddingGemma in Ollama](https://ollama.com/library/embeddinggemma)
