# ADR-0036: Whether oMLX replaces vllm-mlx + llama-swap on the Mac

- **Date:** 2026-10-04 (raised, from an Oct 2026 look at new Mac-tier technology)
- **Status:** **Open.** An exploration, not a commitment. The Mac tier is unbuilt, and it comes after the cluster stack. Lean: oMLX, if it passes the test below once the Mac tier is actually being built. [ADR-0002](0002-vllm-mlx-behind-llama-swap.md) stays the design of record until then.
- **Supersedes / related:** would supersede [ADR-0002](0002-vllm-mlx-behind-llama-swap.md) (the engine and the router, both replaced). Leaves [ADR-0001](0001-native-inference-on-the-mac.md) unchanged: inference stays native, under a LaunchAgent, behind one OpenAI-compatible endpoint. Would change the Mac-tier line of [ADR-0014](0014-observability-managed-backend.md), which names vllm-mlx's `/metrics` as the source. Independent of [ADR-0037](0037-whether-llmkube-manages-mac-models.md) (LLMKube), which would build on this but is not needed for it. Reference text that would change: `../architecture.md` §1 diagram, §2.1 (the LaunchAgent plist), §2.3; the root `README.md` diagram. Code: none yet, because the Mac's Ansible role is unbuilt.

## Context

ADR-0002 runs one vllm-mlx process per model behind llama-swap, which owns
process lifecycle and presents one endpoint on `:8080`. That design is still
unbuilt: `architecture.md` §2 describes the Mac tier as designed but not yet
automated.

The Mac tier was re-evaluated in October 2026. The constraint behind
ADR-0001 hasn't moved. Metal still doesn't pass through to a Linux VM or
container, and kubelet and Calico need Linux, so the Mac is still not a
Kubernetes node and inference stays native on macOS.

What has changed is that one daemon now does what took two components in
ADR-0002. oMLX (`jundot/omlx`, Apache-2.0) is built on vllm-mlx's serving
layer and adds, in one process:

- continuous batching (what ADR-0002 chose vllm-mlx for);
- a tiered KV cache across RAM and SSD;
- multi-model serving of LLMs, VLMs, embedding models and rerankers, with LRU
  eviction (what ADR-0002 needed llama-swap for).

## What adopting it would mean

- **oMLX replaces both vllm-mlx and llama-swap.** One daemon serves every
  model. The per-model processes and the router in front of them go away.
- **Ansible installs it from Homebrew and runs it as one LaunchAgent** in the
  auto-login user's session, **not** a system LaunchDaemon. ADR-0001's reason
  holds unchanged: Metal is most reliable with an active WindowServer session.
- **The boundary to the cluster stays where ADR-0001 put it:** one
  OpenAI-compatible endpoint on `:8080`, so LiteLLM's `api_base` and the
  firewall rule in [ADR-0013](0013-ingress-certs-dns-external-access.md) don't
  change.
- **The warm core survives the change of engine.** ADR-0002's rule "never
  swap the core" (agent, embedding and reranker are always resident) is a
  requirement on oMLX, not a casualty of it. LRU eviction must be able to
  exclude those models. This is part of the test below, not
  something to discover in production: an evicted embedding model means a
  cold start in the middle of retrieval.
- **Pin the oMLX version** and read the release notes on every upgrade.

### What it would take to decide: about a week of soak on real agent traffic

Decide this when the Mac tier is being built, not before. Choose oMLX only
if all of these hold:

1. **No wedges.** Over the soak, the server never stops serving in a way that
   `KeepAlive` doesn't recover from. Recent releases fixed bugs where the
   server wedged, which is why this is the first criterion.
2. **The warm core stays resident.** Agent, embedding and reranker are never
   evicted while experiment models load and unload around them.
3. **Batching holds under concurrency.** Agent loops, RAG retrieval and chat
   run at the same time without being serialised. This was ADR-0002's reason
   for choosing vllm-mlx over Ollama.
4. **Memory headroom holds.** Resident weights plus KV cache stay under
   `iogpu.wired_limit_mb` with slack, as ADR-0002 requires.
5. **Metrics are obtainable** in a form the ADR-0014 collector can scrape (see
   Consequences).

Neither stack is running yet, so this is a test of oMLX before the Mac role
is written around it, not a migration. If it fails, ADR-0002's design is the
fallback, and that is already written down.

## Alternatives rejected

- **Keep vllm-mlx + llama-swap (ADR-0002).** It works, but it is two
  fast-moving components where one now does the job: llama-swap's config
  schema moves, and there is one engine process per model to supervise. It
  also has no shared KV-cache tier, so nothing spills to SSD.
- **MetalBox** (a native process supervisor with Metal memory caps). It
  changes nothing architecturally. It supervises processes but replaces
  neither the engine nor the router, so it would sit alongside ADR-0002's
  design rather than simplify it.
- **Ollama, plain MLX server, LM Studio.** Rejected in ADR-0002 for
  concurrency and headless-operation reasons that still apply.

## Consequences

What gets better:
- One component to install, pin, supervise and upgrade instead of two plus
  one process per model.
- The KV cache can spill to SSD rather than competing purely for wired memory.

What gets genuinely worse:
- **One failure domain.** Under ADR-0002 a crashed model process took down one
  model and llama-swap restarted it. A wedged oMLX daemon takes down every
  model at once, the warm core included. `KeepAlive` restarts a crashed
  process but does not detect a wedged one, so a health check that restarts
  on a hung endpoint is part of the Mac role, not optional.
- **A young project.** oMLX was created in February 2026 and has a high
  issue count. The pin-and-read-release-notes discipline ADR-0002 already
  required for llama-swap now carries more weight.
- **Disk pressure grows.** The SSD KV tier competes with model weights on the
  same disk. ADR-0002 already noted that disk fills faster than memory here,
  so size the cache tier deliberately.

Neutral, contrary to how it was first framed:
- **MLX-format models only (no GGUF, no llama.cpp)** was recorded as a
  trade-off of oMLX. As far as is known it is not a new loss:
  vllm-mlx serves MLX models only too. Confirm this before treating GGUF as
  something being given up.

Elsewhere in the repo:
- **ADR-0014's Mac-tier metrics source is stale if oMLX is chosen**,
  whether or not LLMKube happens. It names vllm-mlx's `/metrics`. If oMLX
  exposes JSON rather than Prometheus metrics, an adapter is needed from the
  first day of the Mac tier, not only under LLMKube.
- **If decided in favour:** record it in a new ADR that answers this one, set ADR-0002 to "Superseded by" that ADR; rewrite
  `architecture.md` §1, §2.1 and §2.3 and the README diagram. ADR-0014 is not
  edited; this record carries the change to its Mac-tier line, and the
  reference text in `architecture.md` §8 is updated instead.

## To verify before deciding

The following came from the October 2026 evaluation and have not yet been
checked against upstream:

- oMLX is built on vllm-mlx's serving layer, and its continuous batching
  matches vllm-mlx's under concurrent load.
- LRU eviction can pin specific models (the warm-core requirement above).
- A Homebrew formula exists and is maintained.
- What metrics oMLX exposes, and in what format.
- vllm-mlx does not serve GGUF (the "neutral" point above).

## Evidence

Nothing built; nothing to verify yet. The soak test above produces this
record's evidence, and the worklog entry for it goes here.
