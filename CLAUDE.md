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

**Standing guardrails (repo-wide, not phase-specific):**
- **No Terraform.** Provisioning is Ansible-only — a deliberate reversal
  (state-file secret handling was the dealbreaker). See Appendix A in the
  design doc before reintroducing it.
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
