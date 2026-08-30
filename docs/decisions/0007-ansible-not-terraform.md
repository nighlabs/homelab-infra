# ADR-0007: Provisioning: Ansible only — Terraform/OpenTofu dropped

- **Date:** 2026-07 (initial design; a reversal of the first draft)
- **Status:** Accepted
- **Supersedes / related:** the original Terraform/bpg plan (recorded below); [ADR-0005](0005-flatcar-k3s-sysext-ignition-config-drive.md); [ADR-0008](0008-flux-via-flux-operator.md); [ADR-0027](0027-control-node-secrets-bws-runtime.md) (where the secrets Ansible reads actually live); `../architecture.md` §3.4, §8

## Context

The original design split tooling by layer: Terraform/OpenTofu with the `bpg`
Proxmox provider owning VM lifecycle, Ignition the node config, Flux the
cluster contents, and Ansible the Mac — with the note "Terraform's
state/plan/drift: don't trade that away."

On reflection, for a single-operator lab of one control plane and three
*static* workers, that bought drift detection and incremental reconciliation
the project does not need, at the cost of a fourth tool and a Terraform state
file. The state file was the specifically-disliked part: it holds secrets in
plaintext and must be stored securely, locked, and never lost.

## Decision

**Ansible owns all provisioning** — Proxmox VM lifecycle, the Flatcar template
build, per-node Ignition generation and delivery, the Mac's configuration, and
bootstrapping Flux as the final step. **No Terraform.**

Ansible's scope:
- Flatcar template build (download the proxmoxve image → import → convert) as
  an idempotent role — this also absorbs what would otherwise have been a
  standalone `make_template.sh`;
- per-node Butane rendered with Jinja2 from the node map, transpiled with
  `butane --strict`, uploaded as a snippet, attached via `cicustom`;
- VM clone/configure/delete via the `community.proxmox` modules, using a
  scoped `ansible@pve` API token (never `root@pam`); SSH to the host only for
  the snippet file operations and `qm`;
- the Flux bootstrap ([ADR-0008](0008-flux-via-flux-operator.md));
- the Mac: Homebrew installs, launchd plists, `sysctl`/`pmset`, vllm-mlx +
  llama-swap, the Prometheus exporters.

Tooling boundaries: Ansible provisions the **VMs** and configures the **Mac**;
Ignition configures the **Flatcar nodes** (Ansible only generates and delivers
it, never converges a running node); Flux delivers **cluster contents**. Each
tool does one job — three tools, not four.

## Alternatives rejected

- **Terraform/OpenTofu + `bpg/proxmox` (the original call).** Rejected for the
  state file above all: a plaintext-secret artifact to store securely, lock,
  and never lose, in a project whose secrets design exists precisely to keep
  such artifacts out of the working tree. Ansible is **stateless** — it queries
  Proxmox for live state instead of persisting one — which removes the problem
  rather than mitigating it. It was also a fourth tool where Ansible was
  already in the stack for the Mac.
- **Keeping Terraform for the "plan / drift" value.** What is given up:
  `terraform plan` previews, drift detection, and rigorous state-diff
  reconciliation. Accepted, because the goal is **consistent rebuild**, not
  drift management — and that survives intact: a version-controlled playbook
  *is* a codified rebuild (Ansible recreates VMs + Ignition → Flux repopulates
  from Git → Ceph + backups hold data). Idempotency becomes module-best-effort
  (Proxmox existence checks) rather than a state diff, which is irrelevant for
  clean-slate rebuilds.
- **`Telmate/proxmox` provider** (kept for history): when Terraform was still
  in scope, `bpg/proxmox` was chosen over the unmaintained Telmate provider.
  Moot now, recorded in case Terraform is ever reconsidered.

## Consequences

- Secrets are never persisted to a state file. The original entry said the
  Bitwarden "secret zero" is seeded transiently from Ansible Vault; that is
  **superseded** — Ansible reads Bitwarden Secrets Manager at run time and
  there is no `vault.yml`. See [ADR-0027](0027-control-node-secrets-bws-runtime.md).
- Rebuild is "re-run the playbook": after a GUI delete or a teardown play the
  cluster comes back identically and Flux repopulates workloads. The source of
  truth is Proxmox queried live, not a state file that can skew, lock, or
  leak.
- Ansible-driving-Flatcar is less documented than Terraform+bpg, so more of
  the role is hand-built. The residual wrinkle that was anticipated —
  `proxmox_kvm` not exposing `cicustom` — did not materialise in the pinned
  collection; attach and detach both go through the module. Prefer the API
  over SSH for anything the token can do; keep SSH to the snippet file itself.
- With provisioning and the Mac both in one Ansible repo, Renovate's regex
  manager can track every pinned version (Flatcar image, k3s sysext,
  vllm-mlx, llama-swap) in one place.
- Pair the Mac's Ansible-managed config with LiteLLM **fallbacks** to a cloud
  model so the cluster degrades gracefully when the Mac is unreachable.
