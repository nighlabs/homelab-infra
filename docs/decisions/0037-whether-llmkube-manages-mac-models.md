# ADR-0037: Whether to manage the Mac's models declaratively with LLMKube

- **Date:** 2026-10-04 (raised, in the Oct 2026 re-evaluation of the Mac tier)
- **Status:** **Open.** An exploration of new technology, not a commitment: no cluster stack exists for it to run in yet. Lean: worth trying, but only after the cluster stack is up and [ADR-0036](0036-whether-omlx-replaces-vllm-mlx-and-llama-swap.md) is decided in oMLX's favour, and only once the open questions below are answered.
- **Supersedes / related:** depends on [ADR-0036](0036-whether-omlx-replaces-vllm-mlx-and-llama-swap.md) (oMLX is the engine LLMKube would supervise). Would **partially supersede** [ADR-0001](0001-native-inference-on-the-mac.md) (the Mac as a passive backend that holds no cluster credential) and [ADR-0013](0013-ingress-certs-dns-external-access.md) (its inter-tier rule: the firewall scopes `:8080` to the cluster). Related: [ADR-0034](0034-secrets-store-1password.md) (where the agent's credential comes from), [ADR-0028](0028-gitops-delivery-signed-oci-syncless-fluxinstance.md) (Flux would deliver the model CRDs), [ADR-0014](0014-observability-managed-backend.md) (metrics).

## Context

Under ADR-0036 the Mac's model set is oMLX configuration that Ansible
templates. LLMKube would instead make each model a Kubernetes resource
(`Model` / `InferenceService` CRDs), delivered by Flux from the same signed
OCI artifact as everything else. The draw is a deterministic, Git-managed model
set that reconciles itself, and Kubernetes-native endpoints for LiteLLM to
target.

The shape that was evaluated:

- **The operator runs in k3s.**
- **A `metal-agent` runs on the Mac** as a LaunchAgent, not as a node. It
  watches `InferenceService`s, supervises oMLX, and registers `EndpointSlice`s
  so the cluster can route to the Mac. The Mac stays on its segmented VLAN.
- **LiteLLM stays the gateway**, pointed at the LLMKube Service instead of at
  the Mac directly. Switching its backend is the only change on the cluster
  side.
- **ModelRouter is skipped.** It's a fail-closed policy layer, which isn't
  needed now.
- **LLMKube is pinned at `>= 0.10.1`.** 0.10.0 was a breaking release: engines
  bind to loopback behind an authenticated relay with per-namespace tokens,
  replacing firewall-scoped port access. 0.10.1 tightened the agent's RBAC to
  least privilege.

## What adopting it would change in existing records

This is the main thing to weigh. LLMKube is not just a
layer on top of ADR-0036: it reverses two properties that earlier records
treat as fixed.

1. **The Mac stops being passive (ADR-0001).** Today the cluster calls the Mac
   and the Mac calls nothing. Under LLMKube the Mac **holds a Kubernetes
   credential and opens connections to the k3s API**. That is new traffic in
   the opposite direction, from the Mac's VLAN to the cluster network, and it
   needs its own firewall rule. It also grows the configured surface of the
   one host ADR-0001 deliberately kept minimal.
2. **Authentication replaces firewall scoping (ADR-0013).** ADR-0013 protects
   the Mac by allowing only the cluster to reach `:8080`. From 0.10.0, engines
   listen on loopback and are reached through an authenticated relay. Decide
   whether the firewall rule stays as a second layer or is retired, and which
   port the relay uses.
3. **The agent's credential can't come from ESO.** ESO creates `Secret`s
   inside the cluster. The metal-agent runs on the Mac, outside the cluster,
   which ADR-0001 keeps out of Kubernetes. So the Mac's half of the credential
   reaches it the way everything else on the Mac does: **Ansible reads
   1Password and writes it into the agent's LaunchAgent configuration.** The
   cluster-side half of the relay secret can still come from ESO once ESO is
   live, from the same 1Password item. As always, nothing is committed.

The credential itself: a dedicated ServiceAccount with **read** access to
`InferenceService`s and **write** access to `EndpointSlice`s, in one
namespace, and nothing else.

## Open questions

- **Does LLMKube's model fit one shared oMLX daemon?** LLMKube expects one
  `InferenceService` per model. oMLX serves every model from one daemon and
  evicts by LRU. How the two interact is undocumented. If the agent assumes it
  controls each model's lifecycle independently, oMLX's eviction can undo
  what the CRDs declare, which would defeat the "deterministic" reason for
  adopting it. Test pinning under the agent before relying on it.
  (Whether oMLX can pin models *at all* is ADR-0036's gate, not this one's.)
- **Where the agent's token comes from:** a long-lived ServiceAccount token
  stored in 1Password and rotated by re-running Ansible, or a short-lived
  token Ansible requests from the cluster (TokenRequest) at run time, which
  then needs a refresh mechanism on the Mac.
- **Upgrade churn.** LLMKube is pre-1.0, and breaking changes arrive fast
  (0.10.0 changed the network model). Pin it and read the release notes on
  every upgrade, as with oMLX.
- **Metrics.** Under LLMKube, oMLX's metrics are reported as JSON only, so
  the ADR-0014 collector needs a scrape adapter. This mostly reduces to
  ADR-0036's metrics question; what's open here is whether LLMKube adds
  anything worth scraping.

## Options

- **Adopt LLMKube, staged (the lean).** Test it on one non-critical model
  alongside the direct LiteLLM-to-Mac path, then migrate. Accept the
  ADR-0001/0013 reversals above explicitly, in this record's successor.
- **Stay with Ansible-templated oMLX configuration.** The honest
  counterpoint: that configuration is *already* in Git and already
  deterministic, since Ansible renders it from the repo. What LLMKube adds is
  continuous reconciliation, Flux delivery alongside the rest of the cluster,
  and Kubernetes-native endpoints. What it costs is a Kubernetes credential on
  the Mac and a pre-1.0 dependency. Whether that trade is worth it is the
  actual question.

## Staging, if adopted

1. Build the Mac tier with oMLX through Ansible (ADR-0036). LiteLLM points at
   the Mac directly.
2. Bring up the cluster stack as already planned.
3. Add LLMKube with one non-critical model alongside the existing path, then
   migrate. Changing LiteLLM's backend from the Mac's address to the cluster
   Service is the only switch.
4. Decide this record. On adoption, record in the deciding ADR that
   ADR-0001 and ADR-0013 are partially superseded, and update their index
   rows to match.

## Evidence

Nothing built; nothing to verify. The LLMKube version history, the agent's
RBAC, and the relay model come from the October 2026 evaluation and have not
yet been checked against upstream release notes. Re-check them before acting.
