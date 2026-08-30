# ADR-0014: Observability: vendor-neutral collector in-cluster, managed backend out-of-band

- **Date:** 2026-07 (initial design)
- **Status:** Accepted — not yet implemented
- **Supersedes / related:** [ADR-0001](0001-native-inference-on-the-mac.md) (the Mac tier is scraped over the LAN); [ADR-0009](0009-secrets-aescbc-and-eso-bitwarden.md) (same anti-lock-in lever); `../architecture.md` §8

## Context

A cluster outage is exactly when metrics are needed most, so an
observability stack that shares the cluster's fate is worth less than it
looks. Hosting Prometheus/Loki/Grafana is also real operational weight for a
single operator. The volume at homelab scale is small, but a default
Kubernetes metric set blows through active-series caps quickly.

## Decision

- **In-cluster: only a lightweight collector** — an **OpenTelemetry Collector**
  (or Grafana Alloy). It scrapes the Prometheus endpoints (ServiceMonitors /
  PodMonitors, plus the Mac over the LAN), collects logs, and exports via
  **OTLP / Prometheus remote-write**.
- **Storage, dashboards, and alerting live in a managed backend**, out-of-band,
  so they survive a cluster outage.
- **Lean choice — New Relic free tier:** 100 GB/mo ingest, full platform, one
  free full user, native OTLP + Prometheus remote-write. Its **ingest-based**
  model sidesteps Grafana Cloud's 10k-active-series cap, which a default k8s
  stack exceeds; a homelab's metric volume stays well under 100 GB — logs are
  the only real consumer.
- **Alternate — Grafana Cloud free:** 10k series / 50 GB logs / 14-day
  retention / 3 users; watch the series cap (scrape selectively / Adaptive
  Metrics); Pro is ~$19/mo + usage if outgrown.
- **Mac tier**, scraped by the in-cluster collector over the LAN: vllm-mlx
  `/metrics` (KV-cache, queue depth, TTFT, end-to-end latency, tokens),
  node_exporter (darwin), and a powermetrics-based exporter for Apple Silicon
  GPU/VRAM — all run as LaunchAgents, Ansible-managed.
- **LLM layer (later):** Honeycomb (wide-event trace debugging) or
  Langfuse/Phoenix for traces, token usage, and evals on the agent/RAG
  pipeline.
- **Stand it up day 1, not during an incident.**

## Alternatives rejected

- **Self-hosted LGTM stack in-cluster.** Only if on-prem purity or long
  retention wins — at the cost of running it, and of it being cluster-fate-
  shared (down when you need it).
- **Picking the backend on features / lock-in.** Because the collector speaks
  OTLP, the backend is repointable with a config change — the same anti-lock-in
  lever as ESO. So pick on free-tier fit, not on lock-in concerns.

## Consequences

- Operational telemetry leaves the network. Accepted for this lab.
- Backend choice is reversible: a collector config change, no workload
  changes.
- The Mac's exporters are part of its Ansible-managed configuration
  ([ADR-0007](0007-ansible-not-terraform.md)) and need the cluster → Mac
  firewall rule to cover their ports, not just `:8080`.
- Renovate (first-class Flux support, plus a regex manager for the Ansible
  repo's pins) keeps the collector and the rest of the stack current — the
  "dependency currency" half of the same platform-completeness pass.
