# CLAUDE.md — homelab-infra (project root)

Self-hosted inference stack: a Mac Studio (native Metal inference) paired
with a Flatcar + k3s Kubernetes cluster (Proxmox HA) for everything
non-GPU. Full design rationale — including Appendix A's decision log for
*why* each tool/topology choice was made — lives in
`docs/mac-studio-inference-stack-2.md`. Read that before re-litigating a
decision that looks arbitrary; it almost certainly isn't.

This file is deliberately short. It holds only what's true regardless of
which part of the repo you're working in. Task-specific detail (current
milestone, unknowns needing a PoC, in-progress implementation notes) lives
in nested `CLAUDE.md` files next to the code they describe — see the map
below. Those load automatically when Claude reads a file in that
subdirectory; this one loads every session.

## Repo map

- `docs/` — design docs. `mac-studio-inference-stack-2.md` is the source of
  truth for architecture and decisions.
- `ansible/` — owns **all** provisioning: Proxmox VM lifecycle, Flatcar
  Ignition generation (Butane/Jinja2), the Mac's config, and bootstrapping
  Flux as the last step. See `ansible/CLAUDE.md` for current task detail.
- `gitops/` — Flux-managed cluster contents (HelmReleases/Kustomizations).
  Doesn't exist meaningfully yet; see `gitops/CLAUDE.md` once it does.

## Facts that don't change per-task

**Network topology (Ceph, confirmed live config):** the real subnets, VLAN
tags, bridge names, and mon addresses live encrypted in the Ansible vault
(`ansible/inventory/group_vars/all/vault.yml`; the variable *structure* is in
the sibling `vars.yml`) — deliberately not committed in cleartext. Described
by role only here:
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
  cluster-facing (SSH, k3s API/pod/service traffic).

  (Real values for all of the above: see the vault vars — `dmz_network`,
  `ceph_public_network`, and the `proxmox_*` set.)

**Secrets, credentials, and topology blinding (decided 2026-08-02).**
Three tiers, one root of trust. **Bitwarden Secrets Manager (cloud-hosted)** is
the durable store for everything; the split is about *who reads it when*:

| Tier | Example | Mechanism |
|---|---|---|
| **Credentials** | Proxmox API token, k3s join token | **BWS, read at run time** by the `bitwarden.secrets` lookup — no `vault.yml` |
| **Bootstrap secrets** | anything needed before ESO exists | Ansible-seeded `Secret` at bootstrap, from the vault |
| **Runtime app secrets** | app passwords, API keys | ESO + Bitwarden SDK Server, per design §6 |
| **Topology (blinding only)** | BGP peer IP/ASN, LB range | Flux `postBuild.substituteFrom` a `Secret` — *placeholders* in Git |

- **Never commit a credential in any form, including ciphertext.** Encrypted
  secrets in Git are permanent, unrotatable without a commit, and unauditable.
  BWS gives rotation, revocation, and audit; use it.
- **✅ DECIDED 2026-08-17 — `vault.yml` is retired; Ansible reads BWS at run
  time.** An earlier revision of this table called `vault.yml` "a materialized
  cache" of BWS. That's overturned: a cache means two secrets to hold (vault
  passphrase *and* BWS token) and two sources that diverge silently — a stale
  cache is byte-indistinguishable from a fresh one. **Secret zero is
  irreducible but relocates**: the BWS access token can never come from BWS, so
  it lives in the **macOS Keychain**, read at task time. Nothing secret then
  remains in the repo directory. Full rationale, the alternatives rejected, and
  why offline provisioning is a non-scenario: **Appendix A, "Control-node
  secrets"** in `docs/mac-studio-inference-stack-2.md`.
- **Topology is blinded with `${var}` placeholders + post-build substitution**,
  not SOPS. Nothing encrypted is committed, values change without a commit, and
  diffs stay readable. Reach for **SOPS/age only** where substitution can't go
  (whole blocks/lists, or values needed at kustomize-*build* time) — the age
  private key is then just another BWS secret, Ansible-seeded as `sops-age`.
- **ESO cannot be pulled earlier in the chain** — the Bitwarden SDK Server needs
  a cert-manager cert, which needs a Gateway, which needs a LoadBalancer IP.
  That cycle is real, so anything needed before ESO exists is Ansible-seeded.
  Don't try to solve it by moving ESO up.

**Standing guardrails (repo-wide, not phase-specific):**
- **No Terraform.** Provisioning is Ansible-only — a deliberate reversal
  (state-file secret handling was the dealbreaker). See Appendix A in the
  design doc before reintroducing it.
- **No MetalLB.** Calico BGP owns *both* LoadBalancer IP advertisement and the
  pod dataplane (decided 2026-08-02, settling `ansible/CLAUDE.md` §7 item 13).
- **Calico `v3.32.1` + k3s `v1.36.x`** — both current stable, version-matched.
  **Calico 3.32 ships a broken LoadBalancer-IPAM RBAC grant
  ([#12890](https://github.com/projectcalico/calico/issues/12890), open,
  unfixed in 3.32.1), so a workaround ClusterRole MUST be applied.** It is not
  optional and it is not reactive — without it, LoadBalancer IPs sit `pending`
  forever *while BGP advertises the routes normally*, so the failure gives no
  hint on the BGP side. Manifest, rationale, and removal criteria:
  `ansible/CLAUDE.md` §7 item 15.
- **Keep k3s on a supported Kubernetes minor.** Upstream maintains only the
  latest three; we drifted to an EOL 1.32 without noticing. Bump via
  `k3s_minor`/`k3s_version` + re-provision (never sequential in-place upgrades
  while the cluster is disposable). See `ansible/CLAUDE.md` §7 item 16.
- **No DHCP anywhere in cluster networking.** All node addressing is
  static, defined in Ignition, sourced from the Ansible inventory node map.
- **No second Ceph in-cluster.** Always the existing external Proxmox Ceph
  via ceph-csi.
- **No CGNAT (`100.64.0.0/10`) for any cluster CIDR** — collides with
  Tailscale and Cloudflare reservations. Cluster CIDRs live in
  `10.0.0.0/8` (`10.42/16` pods, `10.43/16` services).
- **This Ceph cluster already serves live Proxmox VM storage today** — any
  change to it (mons, networks, pools) affects existing production
  workloads, not just this project. Treat it with the caution that implies.
