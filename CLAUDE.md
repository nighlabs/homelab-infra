# CLAUDE.md — homelab-infra (project root)

Self-hosted inference stack: a Mac Studio (native Metal inference) paired
with a Flatcar + k3s Kubernetes cluster (Proxmox HA) for everything
non-GPU.

**Documentation is split by kind — keep it that way.** `docs/README.md`
explains the split; the short version:

| Kind | Where | Rule |
|---|---|---|
| Reference — how it works now | `docs/architecture.md`, runbooks, each dir's `README.md`/`CLAUDE.md` | Present tense. Rewrite when it changes; never leave strikethroughs or "SUPERSEDED" blocks. |
| Decisions — why, and what was rejected | `docs/decisions/NNNN-*.md` + index | A reversal is a **new** ADR superseding the old. **Read the ADR before re-litigating a choice that looks arbitrary; it almost certainly isn't.** |
| Worklog — what happened when, with evidence | `docs/worklog.md`, newest first | Append-only. Verification evidence, failures found, lessons. |

When you finish a milestone: append a worklog entry, write or update an ADR
if a decision was made, then update the reference text to match. Don't
record status in reference docs.

This file is deliberately short. It holds only what's true regardless of
which part of the repo you're working in. Task-specific detail (current
state, what's next, non-obvious facts) lives in nested `CLAUDE.md` files
next to the code they describe; those load automatically when Claude reads
a file in that subdirectory.

## Repo map

- `docs/` — architecture, decisions, worklog, runbooks. Map: `docs/README.md`.
- `ansible/` — owns **all** provisioning: Proxmox VM lifecycle, Flatcar
  Ignition generation (Butane/Jinja2), Calico priming, Flux bootstrap as the
  last step, and (later) the Mac. See `ansible/CLAUDE.md`.
- `gitops/` — Flux-managed cluster contents, built into a signed OCI
  artifact by CI. See `gitops/CLAUDE.md`.

## Facts that don't change per-task

**Network topology (Ceph, confirmed live config):** the real subnets, VLAN
tags, bridge names, and mon addresses live in Bitwarden Secrets Manager and
reach the repo only as `{{ bws.* }}` references (the variable *structure* is
in `ansible/inventory/group_vars/all/vars.yml`) — deliberately not committed
in any form. Described by role only here:
- Ceph **public network** — mons + all client I/O, incl. ceph-csi. A
  dedicated VLAN on the jumbo (`mtu 8996`) bond. This is the only Ceph
  network any k3s node / ceph-csi client ever touches.
- Ceph **cluster network** — OSD-to-OSD replication only, never
  client-facing. **Untagged/native on that *same* bond**, so leaving the
  secondary NIC untagged silently lands it on replication traffic instead of
  the public network — the tag is what keeps clients on the right one.
- **Management** — separate 1Gb bond, general/management only. Ceph does not
  use this network.
- **DMZ / k3s cluster network** — a dedicated VLAN on the 1Gb bond;
  cluster-facing (SSH, k3s API/pod/service traffic, BGP peering).
- **LoadBalancer range** — one routed-only `/24` per cluster, attached to no
  interface anywhere; pfSense learns it over BGP.

**Secrets, credentials, and topology blinding.** Bitwarden Secrets Manager
(cloud-hosted) is the durable store for everything; the split is about *who
reads it when* (`docs/decisions/0027-control-node-secrets-bws-runtime.md`,
`0009-secrets-aescbc-and-eso-bitwarden.md`, `0021-topology-blinding-postbuild-substitution.md`):

| Tier | Example | Mechanism |
|---|---|---|
| **Credentials** | Proxmox API token, k3s join token | **BWS, read at run time** by the custom bulk-fetch module; secret zero (the BWS token) is a **macOS Keychain** item |
| **Bootstrap secrets** | anything needed before ESO exists | Ansible-seeded `Secret` at bootstrap, from BWS |
| **Runtime app secrets** | app passwords, API keys | ESO + Bitwarden SDK Server, from a *separate* BWS project |
| **Topology (blinding only)** | BGP peer IP/ASN, LB range, node IPs | Flux `postBuild.substituteFrom` the Ansible-seeded `cluster-topology` `Secret` — *placeholders* in Git |

- **Never commit a credential in any form, including ciphertext.** Encrypted
  secrets in Git are permanent, unrotatable without a commit, and unauditable.
- **There is no `vault.yml`** and no vault passphrase. Nothing secret lives in
  the repo directory.
- **Topology is blinded with `${var}` placeholders + post-build substitution**,
  not SOPS. Reach for **SOPS/age only** where substitution can't go (whole
  blocks/lists, or values needed at kustomize-*build* time).
- **ESO cannot be pulled earlier in the chain** — the Bitwarden SDK Server
  needs a cert-manager cert, which needs a Gateway, which needs a LoadBalancer
  IP, which needs the BGP config. That cycle is real, so anything BGP needs is
  Ansible-seeded **permanently**. Don't try to solve it by moving ESO up.
- **The repo and its OCI artifact are public.** Blinding applies to docs too:
  `${placeholder}` / `x.x.x.N`, never a real address.

**Standing guardrails (repo-wide, not phase-specific):**
- **No Terraform.** Provisioning is Ansible-only — a deliberate reversal
  (`docs/decisions/0007-ansible-not-terraform.md`).
- **No MetalLB.** Calico BGP owns *both* LoadBalancer IP allocation/
  advertisement and the pod dataplane (`0018-calico-bgp-replaces-metallb.md`).
- **Calico `v3.32.1` + k3s `v1.36.x`** — a version-matched pair. **Calico 3.32
  ships a broken LoadBalancer-IPAM RBAC grant
  ([#12890](https://github.com/projectcalico/calico/issues/12890)), so the
  workaround ClusterRole in `gitops/infrastructure/calico-bgp/` MUST be
  applied.** Without it LoadBalancer IPs sit `pending` forever *while BGP
  advertises the routes normally*, so the BGP side gives no hint. Removal
  criteria: `0019-k3s-1.36-calico-3.32.1-version-pair.md`.
- **Keep k3s on a supported Kubernetes minor.** Upstream maintains only the
  latest three. Bump via `k3s_version_default` + re-provision (never sequential
  in-place upgrades while the cluster is disposable). Don't run ahead of the
  CNI's supported Kubernetes.
- **No DHCP anywhere in cluster networking.** All node addressing is static,
  defined in Ignition, sourced from `ansible/inventory/nodes.yml`
  (`0017-static-addressing-no-dhcp.md`).
- **Never put a remote `contents.source:` in Ignition.** The initramfs has no
  network here (no DHCP, and the static config only activates after the
  pivot), so a remote fetch boot-loops the node. Fetch post-pivot from a
  systemd unit instead.
- **No second Ceph in-cluster.** Always the existing external Proxmox Ceph via
  ceph-csi. **That cluster already serves live Proxmox VM storage today** —
  any change to it (mons, networks, pools) affects existing production
  workloads. Treat it with the caution that implies.
- **No CGNAT (`100.64.0.0/10`) for any cluster CIDR** — collides with
  Tailscale and Cloudflare reservations. Cluster CIDRs live in `10.0.0.0/8`
  (`10.42/16` pods, `10.43/16` services).
- **Flatcar nodes have no Python.** Ansible tasks that run *on* a node use
  `raw`; everything k8s-side runs from `localhost` via the kubeconfig.
