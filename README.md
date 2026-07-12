# homelab-infra

Infrastructure-as-code for a self-hosted inference stack: a **Mac Studio** running
models natively on Metal, paired with a **Flatcar + k3s** Kubernetes cluster
(VMs on an existing Proxmox HA cluster) that runs everything else.

Metal can't be passed through to a VM or container, so inference has to run on the
host. Everything that isn't inference — routing, storage, app code, UI,
observability — has no such constraint and belongs in Kubernetes, where it's
reproducible and isolated. The two tiers meet at a single OpenAI-compatible HTTP
endpoint.

```
clients / agents
      │
      ▼
┌─ Kubernetes tier — Flatcar + k3s, VMs on Proxmox HA (no GPU) ─┐
│  1 control-plane VM (tainted · SQLite/kine · no etcd) + 3 workers
│  LiteLLM gateway · Postgres · Redis · Qdrant · RAG/agent · Open WebUI
│  storage: ceph-csi → the existing Proxmox Ceph (RBD + CephFS)
│  provisioning: Ansible          GitOps: FluxCD
└───────────────────────────────────────────────────────────────┘
      │  LAN — segmented VLAN, firewall scopes :8080 to the cluster
      ▼
┌─ Mac Studio (256 GB) — native, Metal ─────────────────────────┐
│  llama-swap (one stable endpoint :8080) → vllm-mlx per model
└───────────────────────────────────────────────────────────────┘
```

Full design and rationale — including **Appendix A**, the decision log explaining
why each tool and topology choice was made — lives in
[`docs/mac-studio-inference-stack-2.md`](docs/mac-studio-inference-stack-2.md).
Read that before re-litigating a decision that looks arbitrary; it almost
certainly isn't.

## Repo layout

| Path | Contents |
|---|---|
| `docs/` | Design docs. `mac-studio-inference-stack-2.md` is the source of truth for architecture and decisions. |
| `ansible/` | Owns **all** provisioning: Proxmox VM lifecycle, Flatcar Ignition generation (Butane + Jinja2), k3s install, the Mac's config, and bootstrapping Flux as the last step. See [`ansible/README.md`](ansible/README.md) to actually run it. |
| `gitops/` | Flux-managed cluster contents — `deployment/` entrypoints, `infrastructure/` controllers, `apps/` workloads. Currently holds Calico; the rest arrives as the infra layer comes up. |

Each directory also carries a `CLAUDE.md` with in-progress task state and the
findings behind the current implementation.

## Current status

- **Flatcar VM shell — done.** Two NICs (DMZ + Ceph public, the latter at MTU
  8996), a separate data disk, key-only SSH, fully static addressing. Verified on
  `snoop-a2o`, including an unattended reboot and a from-scratch rebuild.
- **k3s all-in-one server — done.** Installed via the Flatcar k3s sysext, baked
  into Ignition. API serving, secrets-encryption on from boot 1, datastore on the
  data disk (mounted at k3s's default `/var/lib/rancher`, not a `data-dir`
  override). The node stays `NotReady` until its CNI arrives — expected.
- **Calico — implemented, pending a live run.** `bootstrap-cluster.yml` waits for
  k3s, fetches and rewrites the kubeconfig, and primes Calico via the
  tigera-operator Helm chart so the node goes Ready.
- **Next: Flux bootstrap** (Flux Operator + `FluxInstance` + secret-zero), which
  then *adopts* the Ansible-primed Calico release.
- **After that**, from Git in dependency order: MetalLB → NGINX Gateway Fabric +
  cert-manager → ceph-csi-operator + StorageClasses → External Secrets Operator +
  Bitwarden SDK Server → Postgres + Redis → LiteLLM → Qdrant → RAG → Open WebUI →
  OTel.

## Quick start

Provisioning runs from a control node with `uv`, `butane`, and `helm` installed,
plus a scoped Proxmox API token and an SSH user on the PVE host. The full
prerequisite list, the one-time Proxmox user/token setup, and the
definition-of-done checks live in [`ansible/README.md`](ansible/README.md) — start
there. The short version, from the repo root:

```bash
uv sync
cd ansible
uv run ansible-galaxy collection install -r requirements.yml
cp inventory/group_vars/vault.example.yml inventory/group_vars/all/vault.yml
# fill in the vault, then:
uv run ansible-vault encrypt inventory/group_vars/all/vault.yml
uv run ansible-playbook site.yml --ask-vault-pass
```

`site.yml` builds the Flatcar template, provisions every node in
`inventory/nodes.yml` (k3s bakes in via Ignition), then waits for k3s and primes
Calico.

## Network topology

The real subnets, VLAN tags, bridge names, and mon addresses are **not committed
in cleartext** — they live encrypted in `ansible/inventory/group_vars/all/vault.yml`
(the variable *structure* is in the sibling `vars.yml`). By role:

- **Ceph public network** — mons and all client I/O, including ceph-csi. A
  dedicated VLAN on the jumbo (`mtu 8996`) bond. The only Ceph network a k3s node
  ever touches.
- **Ceph cluster network** — OSD-to-OSD replication only, never client-facing.
  Untagged/native on that *same* bond — so leaving the secondary NIC untagged
  silently lands it on replication traffic instead of the public network. The VLAN
  tag is what keeps clients on the right one.
- **Management** — a separate 1Gb bond, general/management only. Ceph does not use
  it.
- **DMZ / k3s cluster network** — a dedicated VLAN on the 1Gb bond; cluster-facing
  (SSH, k3s API, pod and service traffic).

## Guardrails

Repo-wide, not phase-specific. Each is a decision already made, with the reasoning
in Appendix A of the design doc:

- **No Terraform.** Provisioning is Ansible-only — a deliberate reversal, with
  state-file secret handling as the dealbreaker.
- **No DHCP anywhere in cluster networking.** All node addressing is static,
  defined in Ignition, sourced from the Ansible node map (`inventory/nodes.yml` is
  the sole source of truth for address allocation).
- **No second Ceph in-cluster.** Always the existing external Proxmox Ceph via
  ceph-csi. That cluster already serves live Proxmox VM storage today — any change
  to its mons, networks, or pools affects production workloads, not just this
  project.
- **No CGNAT (`100.64.0.0/10`) for any cluster CIDR** — it collides with Tailscale
  and Cloudflare reservations. Cluster CIDRs live in `10.0.0.0/8`
  (`10.42.0.0/16` pods, `10.43.0.0/16` services).
- **Never put a remote `contents.source:` in Ignition.** The initramfs has no
  network here (no DHCP, and the static config only activates after the pivot), so
  a remote fetch boot-loops the node. Fetch post-pivot from a systemd unit instead.

## License

See [LICENSE](LICENSE).
