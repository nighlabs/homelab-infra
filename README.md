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
│  Calico: eBPF dataplane, BGP-routed pods, LB IPs advertised to pfSense
│  LiteLLM gateway · Postgres · Redis · Qdrant · RAG/agent · Open WebUI
│  storage: ceph-csi → the existing Proxmox Ceph (RBD + CephFS)
│  provisioning: Ansible     delivery: Flux ← cosign-signed OCI artifact
└───────────────────────────────────────────────────────────────┘
      │  LAN — segmented VLAN, firewall scopes :8080 to the cluster
      ▼
┌─ Mac Studio (256 GB) — native, Metal ─────────────────────────┐
│  llama-swap (one stable endpoint :8080) → vllm-mlx per model
└───────────────────────────────────────────────────────────────┘
```

## Documentation

Start at [`docs/README.md`](docs/README.md) — it maps the set. The short version:

| Read | For |
|---|---|
| [`docs/architecture.md`](docs/architecture.md) | How it works — the design and what's built, present tense |
| [`docs/decisions/`](docs/decisions/README.md) | **Why** — one record per decision, with alternatives rejected and status. Read the relevant ADR before re-litigating a choice that looks arbitrary; it almost certainly isn't. |
| [`docs/worklog.md`](docs/worklog.md) | What happened when, with the evidence |
| [`ansible/README.md`](ansible/README.md) | How to run it: prerequisites, one-time setup, the plays, verification, troubleshooting |
| [`gitops/CLAUDE.md`](gitops/CLAUDE.md) · [`ansible/CLAUDE.md`](ansible/CLAUDE.md) | Working notes for each subtree — layout, non-obvious facts, current task |

## Repo layout

| Path | Contents |
|---|---|
| `docs/` | Architecture, decision records, worklog, runbooks. |
| `ansible/` | Owns **all** provisioning: Proxmox VM lifecycle, Flatcar Ignition generation (Butane + Jinja2), k3s install, Calico priming, and bootstrapping Flux as the last step. |
| `gitops/` | Flux-managed cluster contents — `deployment/` entrypoints, `crds/`, `infrastructure/` controllers, `apps/` workloads. Built into a signed OCI artifact by `.github/workflows/gitops-artifact.yml`. |

## Current status (2026-08-30)

The cluster tier's foundation is **live on one node** (`snoop-a2o`, an
all-in-one k3s server), all of it from a single from-scratch
`ansible-playbook site.yml`:

- **Flatcar VM** — two NICs (DMZ + Ceph public at MTU 8996), separate data
  disk, key-only SSH, fully static addressing; the Ignition snippet is destroyed
  after first boot.
- **k3s `v1.36.2`** via the Flatcar sysext, secrets-encryption on from boot 1,
  unattended patch updates proven.
- **Calico `v3.32.1`** — eBPF dataplane (no kube-proxy), BGP-routed pods with no
  encapsulation, LoadBalancer IPs allocated by Calico and advertised over BGP to
  pfSense/FRR; reachable from another segment.
- **Flux `2.9.x`** via the Flux Operator, reconciling a **cosign-signed OCI
  artifact** (`SourceVerified=True`); it adopted the Ansible-primed Calico
  without a diff war.

**Next:** the rest of the stack from Git in dependency order — NGINX Gateway
Fabric + cert-manager → ceph-csi → External Secrets Operator + Bitwarden SDK
Server → Postgres + Redis → LiteLLM → Qdrant → RAG → Open WebUI → OTel. Then the
control-plane taint and worker nodes, then the Mac tier. Detail in
[`docs/architecture.md` §7](docs/architecture.md#7-bring-up-order--built-and-remaining).

## Quick start

Provisioning runs from a control node with `uv`, `butane`, and `helm` installed,
a scoped Proxmox API token, an SSH user on the PVE host, and a Bitwarden Secrets
Manager access token in the macOS Keychain. The full prerequisite list, the
one-time Proxmox and BWS setup, and the definition-of-done checks live in
[`ansible/README.md`](ansible/README.md) — start there. The short version, from
the repo root:

```bash
uv sync
cd ansible
uv run ansible-galaxy collection install -r requirements.yml
# create the BWS project + secrets per BWS-SECRETS.md, put the token in the Keychain, then:
uv run ansible-playbook site.yml
```

`site.yml` builds the Flatcar template, provisions every node in
`inventory/nodes.yml` (k3s bakes in via Ignition), waits for k3s and primes
Calico, then bootstraps Flux — which takes ownership from there. **There is no
`vault.yml`**; secrets are read from BWS at run time.

## Network topology

The real subnets, VLAN tags, bridge names and addresses are **not committed in
any form** — they live in Bitwarden Secrets Manager and reach the repo only as
`{{ bws.* }}` references and `${var}` placeholders. By role:

- **DMZ / k3s cluster network** — a dedicated VLAN on the 1Gb bond;
  cluster-facing (SSH, k3s API, pod and service traffic, BGP peering).
- **Ceph public network** — mons and all client I/O, including ceph-csi. A
  dedicated VLAN on the jumbo (`mtu 8996`) bond. The only Ceph network a k3s node
  ever touches.
- **Ceph cluster network** — OSD-to-OSD replication only, never client-facing.
  Untagged/native on that *same* bond — so leaving the secondary NIC untagged
  silently lands it on replication traffic. The VLAN tag is what keeps clients on
  the right one.
- **Management** — a separate 1Gb bond, general/management only. Ceph does not use
  it.
- **LoadBalancer range** — one routed-only `/24` per cluster, attached to no
  interface anywhere; pfSense learns it over BGP.

## Guardrails

Repo-wide, not phase-specific. Each is a decision already made; the reasoning is
in the linked record:

- **No Terraform.** Provisioning is Ansible-only — a deliberate reversal, with
  state-file secret handling as the dealbreaker ([ADR-0007](docs/decisions/0007-ansible-not-terraform.md)).
- **No MetalLB.** Calico BGP owns both LoadBalancer IP allocation/advertisement
  and the pod dataplane ([ADR-0018](docs/decisions/0018-calico-bgp-replaces-metallb.md)).
- **No DHCP anywhere in cluster networking.** All node addressing is static,
  defined in Ignition, sourced from `inventory/nodes.yml` — the sole source of
  truth for address allocation ([ADR-0017](docs/decisions/0017-static-addressing-no-dhcp.md)).
- **No second Ceph in-cluster.** Always the existing external Proxmox Ceph via
  ceph-csi. That cluster already serves live Proxmox VM storage — any change to
  its mons, networks, or pools affects production workloads ([ADR-0006](docs/decisions/0006-ceph-csi-external-proxmox-ceph.md)).
- **No CGNAT (`100.64.0.0/10`) for any cluster CIDR** — it collides with Tailscale
  and Cloudflare reservations. Cluster CIDRs live in `10.0.0.0/8`
  (`10.42.0.0/16` pods, `10.43.0.0/16` services) ([ADR-0011](docs/decisions/0011-cluster-cidrs-never-cgnat.md)).
- **Never commit a credential in any form, including ciphertext.** Secrets come
  from Bitwarden Secrets Manager at run time; topology is blinded with `${var}`
  placeholders ([ADR-0027](docs/decisions/0027-control-node-secrets-bws-runtime.md),
  [ADR-0021](docs/decisions/0021-topology-blinding-postbuild-substitution.md)).
- **Never put a remote `contents.source:` in Ignition.** The initramfs has no
  network here, so a remote fetch boot-loops the node. Fetch post-pivot from a
  systemd unit instead.

## License

See [LICENSE](LICENSE).
