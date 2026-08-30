# ADR-0002: Inference engine: vllm-mlx per model behind llama-swap

- **Date:** 2026-07 (initial design)
- **Status:** Accepted
- **Supersedes / related:** [ADR-0001](0001-native-inference-on-the-mac.md); `../architecture.md` §2.3–§2.4, §5

## Context

The consumers of inference are concurrent — agent loops, RAG retrieval, a chat
UI, all at once. The engine has to scale under that concurrency rather than
serialise it. At the same time 256 GB is finite, so the set of resident models
has to be managed: some must always be warm, some are experiments that should
free memory on their own.

## Decision

- **vllm-mlx** (or the official **vllm-metal** plugin) is the per-model engine:
  continuous batching + paged KV cache on Metal, OpenAI-compatible. One process
  per model.
- **llama-swap** sits in front as the single stable endpoint (`:8080`) and owns
  process lifecycle: start a model's backend on first request, health-check it,
  unload idle models after a TTL.
- The model set is **warm core + swappable experiments**:
  - `warm` group (`swap: false`, `ttl: 0`): the agent/chat model, the embedding
    model, and a reranker. Never evicted. Retrieval and agent loops never pay a
    cold start.
  - `experiments` group (`swap: true`, per-model TTL): only one loaded at a
    time; memory frees itself.
- All weights live on the Mac's local SSD in **one** Hugging Face cache
  (`HF_HOME`), set in the LaunchAgent env too, so multi-GB files are never
  duplicated.

## Alternatives rejected

- **Ollama.** Wraps llama.cpp; even with `OLLAMA_NUM_PARALLEL` it queues rather
  than doing true continuous batching, so it does not scale under concurrent
  agents/RAG. Fine single-stream, non-scaling beyond that.
- **Plain MLX server.** Concurrency is basic.
- **LM Studio.** GUI-oriented; not a headless server component.
- **A single long-running server with all models loaded.** Loses load-on-
  demand / unload-idle, which is what makes the experiments tail affordable.

## Consequences

- **Never swap the core.** The warm group is the reason the rest of the stack
  can assume low-latency retrieval.
- `healthCheckTimeout` must be generous (multi-GB loads take tens of seconds
  before first proxy). Cold starts are why warm-core models never get a TTL.
- `iogpu.wired_limit_mb` is set so the sum of resident weights + KV cache stays
  comfortably under it, with the OS reserve outside it. KV cache under
  continuous batching grows with concurrency and context length — leave slack
  rather than packing to the limit; over-wiring causes beachballs, hard locks,
  or a reset.
- **Version churn:** llama-swap and vllm-mlx both ship fast and the llama-swap
  config schema moves. Pin versions in the LaunchAgent/automation and
  re-validate the config on upgrade.
- Disk fills faster than memory here; the single-cache discipline matters more
  than the RAM figure suggests.
