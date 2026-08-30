# ADR-0001: Inference runs natively on the Mac Studio; everything else runs in Kubernetes

- **Date:** 2026-07 (initial design)
- **Status:** Accepted
- **Supersedes / related:** [ADR-0002](0002-vllm-mlx-behind-llama-swap.md) (the engine that runs there), [ADR-0013](0013-ingress-certs-dns-external-access.md) (how the two tiers reach each other); `../architecture.md` §1–§2

## Context

The stack has one GPU-bound workload — LLM inference — and a long tail of things
that are not: routing, storage, app code, UI, observability. The GPU is a Mac
Studio (256 GB unified memory, Metal). The rest of the lab is a Proxmox HA
cluster with shared Ceph.

macOS cannot pass Metal through to a VM or a container. Docker, Apple
`container`, and Podman all run their workloads inside a Linux VM that has no
Metal device. The one shim that exists (Vulkan via Podman) is slow and fragile.

## Decision

Two tiers, meeting at a single OpenAI-compatible HTTP boundary:

- **Tier 1 — the Mac Studio, native.** Inference only. Models run directly on
  macOS under `launchd`, with the GPU wired-memory limit raised
  (`iogpu.wired_limit_mb`) and macOS hardened into an unattended server
  (never sleep, auto-login, SSH, Spotlight and background services off). The
  acid test is an unattended `sudo reboot` that comes back fully reachable with
  no monitor, keyboard, or person present.
- **Tier 2 — Kubernetes (Flatcar + k3s VMs on Proxmox HA).** Everything that is
  *not* inference. Nothing here needs a GPU, so it belongs where it is
  reproducible, isolated, and rebuildable from Git.

The cluster reaches the Mac at a stable LAN name on `:8080`; the Mac is a dumb
private backend, never exposed publicly. The auth boundary is the LiteLLM
gateway in the cluster.

## Alternatives rejected

- **Containerised inference (Docker / Apple `container` / Podman).** All of
  them run in a VM with no Metal. Inference in a container on macOS means CPU
  inference, which defeats the hardware.
- **Vulkan-via-Podman shim.** Exists, but slow and fragile — not something to
  build a stack on.
- **Everything native on the Mac.** Possible, but the non-inference components
  gain nothing from Metal and lose reproducibility and isolation. A hand-
  configured macOS host is the one mutable machine in the design; keeping its
  surface to "the inference process" keeps that mutability contained.

## Consequences

- The Mac is the one host that is configured rather than provisioned. Ansible
  manages it ([ADR-0007](0007-ansible-not-terraform.md)) so the configuration
  is at least codified.
- Metal is most reliable with an active WindowServer session, so inference runs
  as a **LaunchAgent** in the auto-login user's session, not a system
  LaunchDaemon. That in turn means **FileVault must be disabled** for
  unattended reboot to work (FileVault blocks auto-login) — accepted for a box
  on a private network.
- The inter-tier hop is LAN, not an overlay ([ADR-0013](0013-ingress-certs-dns-external-access.md)).
  If the Mac ever leaves the LAN, repoint LiteLLM's `api_base` at a tailnet
  name — a one-line change bought later, not paid for now.
- Metal under sustained load can be flaky on some macOS point releases. Pin
  macOS and tool versions, soak-test under realistic concurrency, and keep
  `KeepAlive` on so a crashed model process restarts. If end-to-end tests
  later fail intermittently, check this before assuming a cluster-side bug.
