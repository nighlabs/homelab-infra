# Architecture — self-hosted inference stack

A two-tier design: everything GPU-bound runs **natively on a Mac Studio** (the
only place it gets Metal), and everything else runs on a **Flatcar + k3s
Kubernetes cluster** as VMs on the existing Proxmox HA cluster. The two tiers
talk over the LAN using OpenAI-compatible HTTP.

This document describes the system **as designed and, where it exists, as
built** — present tense, no history. The reasoning behind each choice, with the
alternatives rejected, is in [`decisions/`](decisions/README.md) (linked inline
as ADR-NNNN). What was done when, with the evidence, is in
[`worklog.md`](worklog.md). Where a section describes something not yet built,
it says so.

---

## 1. Topology

```
clients / agents
      │
      ▼
┌─ Kubernetes tier — Flatcar + k3s, VMs on Proxmox HA (no GPU) ──────────────
│  today: 1 all-in-one node (snoop-a2o)   target: 1 tainted CP + 3 workers
│  datastore: SQLite via kine (no etcd)   HA: Proxmox restarts the VM
│  CNI: Calico, eBPF dataplane, BGP-routed pods (no encap), no kube-proxy
│  LB IPs: Calico LoadBalancer IPAM, advertised over BGP to pfSense (FRR)
│  ingress: NGINX Gateway Fabric (Gateway API), one shared Gateway
│  planned: cert-manager · ceph-csi · ESO
│           Postgres · Redis · Qdrant · LiteLLM · RAG/agent · Open WebUI · OTel
│  storage: ceph-csi → the existing Proxmox Ceph (RBD + CephFS)
│  provisioning: Ansible    delivery: Flux, from a cosign-signed OCI artifact
└─ LAN — segmented VLAN; firewall scopes :8080 to the cluster ────────────────
      │
      ▼
┌─ Mac Studio (256 GB) — native, Metal ──────────────────────────────────────
│  llama-swap  (one stable endpoint :8080)
│    warm group (swap:false, no TTL) : agent · embedding · reranker
│    experiments (swap:true, TTL)    : models being tried out
│  backend per model : vllm-mlx (continuous batching on Metal)
└────────────────────────────────────────────────────────────────────────────
```

**Why this split:** Metal can't be passed through to a container or VM, so
inference must run natively on the host ([ADR-0001](decisions/0001-native-inference-on-the-mac.md)).
Everything that *isn't* inference — routing, storage, app code, UI,
observability — has no such constraint and belongs in Kubernetes, where it's
reproducible and isolated. The two tiers meet at a single HTTP boundary.

**Current state (2026-08-30):** the cluster tier's foundation is live on one
node — Flatcar VM, k3s, Calico with BGP + eBPF, LoadBalancer IPs routed via
pfSense, and Flux reconciling a signed OCI artifact — all from a single
from-scratch `ansible-playbook site.yml`. The Mac tier is designed but not yet
automated. Everything downstream of Calico in the cluster (Gateway onward) is
the next milestone. See §7.

---

## 2. Tier 1 — Mac Studio inference node

*Status: designed; not yet automated. The Ansible role for the Mac is future
work ([ADR-0007](decisions/0007-ansible-not-terraform.md) gives Ansible the Mac
as well as the VMs).*

### 2.1 Turn macOS into a server

macOS has no official "server mode", but four layers make it behave like one.
The acid test: an unattended `sudo reboot` that comes back fully reachable with
no monitor, keyboard, or person present.

**Never sleep**
```bash
sudo pmset -a sleep 0 displaysleep 0 disablesleep 1 powernap 0 womp 1
# womp 1 = wake on network; powernap 0 = no background wake churn
```

**Auto-login** (so a GUI session exists on boot — Metal is most reliable with an
active WindowServer session): System Settings → Users & Groups → automatic login
for the service user. **FileVault blocks auto-login.** Decide deliberately:
disable FileVault and unattended reboot after a power cut works; keep it and
every cold boot needs a manual unlock. For an inference box on a private
network the usual call is to disable it; if the data on disk matters, keep it
and accept the manual unlock.

**Remote management**
```bash
sudo systemsetup -setremotelogin on        # SSH
# Enable Screen Sharing in Settings → General → Sharing for the rare GUI task
```

**Trim background services** (each frees unified memory and stops disk/CPU
churn): disable Spotlight (`sudo mdutil -a -i off`); turn off Siri, iCloud sync,
Photos analysis, Time Machine (or at least local snapshots), Handoff,
Notifications; Accessibility → Reduce Motion + Reduce Transparency; strip Login
Items; consider a dedicated low-privilege macOS user that owns the whole stack.

**Run the stack under launchd**, not a terminal left open. Because Metal wants
the logged-in session, inference runs as a **LaunchAgent** in the auto-login
user's session (not a system LaunchDaemon). The `iogpu` sysctl below *is* a good
fit for a LaunchDaemon since it's a kernel tunable.

```xml
<!-- ~/Library/LaunchAgents/local.llama-swap.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.llama-swap</string>
  <key>ProgramArguments</key>
  <array>
    <string>/opt/homebrew/bin/llama-swap</string>
    <string>--config</string><string>/Users/svc/llama-swap.yaml</string>
    <string>--listen</string><string>0.0.0.0:8080</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```
Load with `launchctl load ~/Library/LaunchAgents/local.llama-swap.plist`.

### 2.2 Unlock the memory

By default macOS lets the GPU wire ~75% of unified memory on machines above
36 GB — roughly **192 GB** of 256. Raise it so big models and several resident
models fit, but leave real headroom for the OS and the non-wired parts of each
model process.

```bash
# 224 GB to the GPU, ~32 GB left for macOS + overhead. Tune to taste.
sudo sysctl iogpu.wired_limit_mb=229376
sysctl iogpu.wired_limit_mb                # verify
```

Persist it (it resets to `0` on reboot) via `/etc/sysctl.conf` or a
LaunchDaemon running the `sysctl` at load. Don't go to 100% — too little for the
OS causes beachballs, hard locks, or a reset. Start conservative (32 GB free)
and tighten only after confirming stability under load.

### 2.3 The inference layer: vllm-mlx behind llama-swap

[ADR-0002](decisions/0002-vllm-mlx-behind-llama-swap.md).

- **vllm-mlx** (or the official **vllm-metal** plugin) is the per-model engine:
  continuous batching + paged KV cache on Metal, OpenAI-compatible. This is what
  scales under concurrent agents/RAG, unlike Ollama's sequential queue.
- **llama-swap** sits in front as a single stable endpoint and manages process
  lifecycle: starts a model's backend on first request, health-checks it,
  unloads idle models after a TTL.

The model set is **warm core + swappable experiments**:

```yaml
# llama-swap.yaml — field names are illustrative; verify against the pinned
# llama-swap version, the schema moves fast.
healthCheckTimeout: 300          # multi-GB loads need time before first proxy

groups:
  warm:                          # core stack — always available, never evicted
    swap: false                  # members coexist, no unloading each other
    members: [agent, embed, rerank]
  experiments:                   # the "playing around" tail
    swap: true                   # only one of these loaded at a time

models:
  agent:
    cmd: >
      vllm-mlx serve mlx-community/Qwen3.5-32B-Instruct-4bit
      --port ${PORT} --continuous-batching
    ttl: 0                       # 0 = never auto-unload
  embed:
    cmd: vllm-mlx serve mlx-community/bge-m3 --port ${PORT}
    ttl: 0                       # keep warm — never cold-start mid-retrieval
  rerank:
    cmd: vllm-mlx serve mlx-community/bge-reranker-v2-m3 --port ${PORT}
    ttl: 0

  llama-70b:
    cmd: >
      vllm-mlx serve mlx-community/Llama-3.3-70B-Instruct-4bit
      --port ${PORT} --continuous-batching
    ttl: 1800                    # unload 30 min after last use
  some-experiment:
    cmd: vllm-mlx serve mlx-community/whatever-you-pull --port ${PORT}
    ttl: 900
```

**Never swap the core.** The agent/chat model, the embedding model, and a
reranker live in the `warm` group with `ttl: 0` so retrieval and agent loops
never pay a cold start. Everything being tried out goes in `experiments` with a
TTL so it frees memory on its own. With 256 GB there is room to keep a 70B-class
model plus the embedding/rerank pair warm and still swap experiments underneath.

### 2.4 Storage

All weights on the Mac's local SSD (fast load), in **one** Hugging Face cache so
multi-GB files aren't duplicated: `export HF_HOME=/Users/svc/models` (set in
the LaunchAgent env too). This single-cache discipline matters more than the
RAM suggests — disk fills faster than memory here.

---

## 3. Tier 2 — Kubernetes cluster (everything non-GPU)

### 3.1 Cluster shape

[ADR-0003](decisions/0003-k3s.md), [ADR-0004](decisions/0004-cluster-shape-kine-single-cp-proxmox-ha.md).

- **Host layer:** VMs on the existing 3-host Proxmox HA cluster, on
  shared/replicated Ceph, so a host failure restarts the VM elsewhere.
- **Node layout — target:** 1 dedicated control-plane node (small, 2 vCPU /
  4 GB, **tainted** so no workloads land on it) + 3 workers sized for the
  workload set (RAM-heavy for Qdrant/Postgres). **Today:** a single
  `all-in-one` node, `snoop-a2o`, untainted. Node roles, sizes and identity come
  from `ansible/inventory/nodes.yml`.
- **Distribution:** **k3s** `v1.36.x` (single binary, SQLite datastore).
- **Datastore:** embedded **SQLite via kine** — no etcd. A single control-plane
  node; availability comes from the Proxmox layer, not an etcd quorum.

**k3s server config** — rendered by Ansible into Ignition as
`/etc/rancher/k3s/config.yaml` (`ansible/roles/flatcar_vm/templates/k3s-config.yaml.j2`):

```yaml
flannel-backend: none           # Calico is the CNI
disable-network-policy: true    # …and the NetworkPolicy enforcer (this only
                                #    turns off k3s's kube-router controller)
disable-kube-proxy: true        # Calico's eBPF dataplane handles Services (§4.3)
disable:
  - traefik                     #   → NGINX Gateway Fabric
  - servicelb                   #   → Calico BGP (no MetalLB)
  - local-storage               #   → ceph-csi
disable-helm-controller: true   # Flux owns Helm
cluster-cidr: 10.42.0.0/16      # pinned explicitly; Calico's IPPool must match
service-cidr: 10.43.0.0/16
secrets-encryption: true        # aescbc at rest, on from boot 1
node-ip: <eth0/DMZ ip>          # never the Ceph NIC
advertise-address: <eth0/DMZ ip>
tls-san: [<eth0 ip>, <hostname>, <per-cluster extras>]
token: <per-cluster join token, from BWS>
# node-taint: control-plane=true:NoSchedule   (control-plane role only)
```

**CoreDNS** and **metrics-server** stay. There is **no kube-proxy** — Calico's
eBPF dataplane replaces it ([ADR-0024](decisions/0024-calico-ebpf-dataplane-no-kube-proxy.md)).
Agents will get the minimal join config (server URL + token), not the flags
above — the agent path isn't built yet.

The k3s datastore, embedded containerd and image cache live on a separate
**data disk mounted at `/var/lib/rancher`** — k3s's *default* data-dir root, so
k3s runs stock with no `data-dir` override (an override broke
`k3s secrets-encrypt` and other tooling that assumes the default).

### 3.2 Availability & datastore durability

- **Control-plane HA = Proxmox HA**, not k8s multi-master. If the host dies,
  Proxmox restarts the CP VM on a surviving host; workloads on the agents keep
  running through the brief blip.
- **Upgrades:** while the cluster is disposable, re-provision at the target
  version rather than upgrading in place ([ADR-0019](decisions/0019-k3s-1.36-calico-3.32.1-version-pair.md)).
  Once there's state: snapshot the CP VM → upgrade k3s → verify (or roll back).
  A single-server k3s upgrade is a sub-minute *control-plane* blip, not cluster
  downtime.
- **Patch updates are unattended**: k3s arrives as a Flatcar sysext and
  `systemd-sysupdate` pulls patch releases within the pinned minor on its own
  (proven live; see the worklog). The version in `group_vars` is the **seed**
  for a fresh node, not what a long-running node runs.
- **Datastore durability:** the SQLite DB lives on replicated Ceph. Litestream
  for continuous offsite point-in-time backup is an option, not yet added.
- *Not* dqlite/rqlite: kine needs a single writer with strictly +1 revisions.

### 3.3 Node OS and provisioning

[ADR-0005](decisions/0005-flatcar-k3s-sysext-ignition-config-drive.md),
[ADR-0017](decisions/0017-static-addressing-no-dhcp.md),
[ADR-0025](decisions/0025-destroy-ignition-snippet-after-first-boot.md).

- **Flatcar Container Linux**, provisioned by **Ignition** — no PXE, no
  Ignition server. Each node's Ignition is delivered as the cloud-init
  **config-drive user-data** (`--cicustom "user=<storage>:snippets/<node>.ign"`),
  which the proxmoxve image's OEM reads as its Ignition source. The Proxmox
  cloud-init GUI fields are inert; **everything** — identity, keys, networking,
  the k3s config — is in the Ignition.
- **k3s via the Flatcar k3s sysext** (`flatcar/sysext-bakery`), not a binary in
  `/opt/bin`: immutable, updated by `systemd-sysupdate`, Renovate-trackable.
  The sysext image is downloaded on first boot by a systemd unit, *not* fetched
  by Ignition — the initramfs has no network here (see the rule in
  `ansible/CLAUDE.md`).
- **Static addressing, no DHCP anywhere.** Two NICs per node, both configured
  by `systemd-networkd` units in Ignition, matched by **MAC address** that
  Ansible pins at VM creation. `eth0` is the DMZ/cluster network; `eth1` is the
  Ceph public VLAN at MTU 8996 with no default route (§4.1).
- **The Ignition snippet is destroyed after first boot.** It embeds the k3s
  join token and Ignition reads it exactly once; `provision-nodes.yml` waits for
  SSH, detaches `cicustom`, then deletes the file — in that order.
- **Auto-updates:** Flatcar's default auto-update-and-reboot is in force; a
  k8s-aware policy (FLUO drain-then-reboot) is an open decision
  ([ADR-0030](decisions/0030-flatcar-os-update-policy.md)).

### 3.4 Provisioning — Ansible

[ADR-0007](decisions/0007-ansible-not-terraform.md). **No Terraform.**

Ansible owns VM provisioning (and, later, the Mac). It is **stateless** — it
queries Proxmox for live state rather than persisting a state file — which is
what kills Terraform's state-secret problem. `ansible/site.yml` runs four plays
in dependency order:

| Play | Does |
|---|---|
| `build-template.yml` | download the Flatcar proxmoxve image → import → convert to template (idempotent) |
| `provision-nodes.yml` | per node: render Butane with Jinja2 from the node map → `butane --strict` → upload the `.ign` snippet (SSH) → clone the template, pin MACs, attach disk + `cicustom` (API) → boot → wait for SSH → destroy the snippet |
| `bootstrap-cluster.yml` | per cluster: wait for k3s `/readyz`, fetch + rewrite the kubeconfig, seed the `cluster-topology` Secret, prime Calico's CRDs, the tigera-operator release, the BGP CRs and the #12890 workaround, wait for Ready |
| `flux-bootstrap.yml` | helm-install the flux-operator, apply a sync-less `FluxInstance`, seed the `OCIRepository` + root `Kustomization`, wait for the tiers to go Ready |

- **Auth:** a scoped `ansible@pve` API token (never `root@pam`) for the VM
  lifecycle, plus a `provisioner` SSH user with sudo scoped to `qm` and the
  snippet-dir repair — the snippet upload/delete is a file operation with no
  API. Setup in `ansible/README.md`.
- **Secrets** come from Bitwarden Secrets Manager at run time; secret zero is a
  macOS Keychain item (§3.6).
- **Rebuild:** delete the VM, re-run the play, and the same MAC + IP + hostname
  + k3s come back; Flux then repopulates the cluster. The source of truth is the
  node map + Proxmox queried live, not a state file.
- **pfSense is a render target:** `render-frr-config.yml` generates the FRR
  config and firewall-alias members from the same node map; delivery is a paste
  (pfSense CE has no API). Runbook: [`pfsense-frr-bgp-setup.md`](pfsense-frr-bgp-setup.md).

### 3.5 Persistent storage — ceph-csi against the existing Proxmox Ceph

*Status: designed, not yet deployed.* [ADR-0006](decisions/0006-ceph-csi-external-proxmox-ceph.md).

- Reuse the **Proxmox Ceph** cluster; never a second Ceph inside k8s. That
  cluster serves live production VM storage today — any change to it (mons,
  networks, pools) affects existing workloads.
- Deploy via the **ceph-csi-operator** pointed at the external cluster. Two
  StorageClasses: **`ceph-rbd`** (RWO block) for Postgres/Qdrant/Redis;
  **`cephfs`** (RWX) for shared file access.
- RBD volumes aren't node-bound: a dead worker → the pod reschedules and the
  volume re-attaches on a healthy node. Enable Non-Graceful Node Shutdown (the
  `out-of-service` taint) so RBD detaches from a hard-failed node.
- **Setup checklist:** dedicated Ceph pool + restricted client user for k8s
  (don't touch PVE's VM pool); the second vNIC on the Ceph public VLAN is
  already in place; match the ceph-csi version to the Proxmox Ceph release;
  watch RBD/CephFS image features vs the Flatcar kernel.
- **Upgrade order:** confirm version overlap → upgrade Proxmox Ceph (mons → mgr
  → OSDs, `require-osd-release`) → upgrade ceph-csi via the operator → test a
  PVC.

### 3.6 Secrets

[ADR-0009](decisions/0009-secrets-aescbc-and-eso-bitwarden.md),
[ADR-0027](decisions/0027-control-node-secrets-bws-runtime.md),
[ADR-0021](decisions/0021-topology-blinding-postbuild-substitution.md).

**Bitwarden Secrets Manager (cloud-hosted)** is the durable store for
everything. The split is about *who reads it, when*:

| Tier | Example | Mechanism |
|---|---|---|
| **Control-node credentials** | Proxmox API token, k3s join token, FRR password | **BWS, read at run time** by a custom bulk-fetch module; secret zero (the BWS access token) lives in the **macOS Keychain** |
| **Bootstrap secrets** | anything needed before ESO exists | Ansible-seeded `Secret` at bootstrap, from BWS |
| **Runtime app secrets** | app passwords, API keys | **External Secrets Operator + Bitwarden SDK Server**, from a *separate* BWS project |
| **Topology (blinding only)** | BGP peer IP/ASN, LB range, node IPs | `${var}` placeholders in Git, substituted by Flux from the Ansible-seeded `cluster-topology` Secret |

- **There is no `vault.yml`.** Nothing secret lives in the repo directory in
  any form, including ciphertext. The secret manifest is `ansible/BWS-SECRETS.md`.
- **Datastore at rest:** k3s `secrets-encryption` (aescbc) from boot 1. A
  KMS-as-KEK upgrade is possible later, traded against a cold-start dependency.
- **ESO cannot be pulled earlier in the chain** — the Bitwarden SDK Server needs
  a cert-manager cert → a Gateway → a LoadBalancer IP → BGP config. So anything
  BGP needs is Ansible-seeded, **permanently**, not just at first bootstrap.
- **Two BWS projects, split by consumer:** `homelab-infra` (read by the control
  node) and an apps project (read by ESO, created at that milestone). A cluster
  compromise must not reach the Proxmox token.
- **Undefined `${var}` substitutes to the empty string and reconciles green.**
  The kustomize-controller feature gate `StrictPostBuildSubstitutions=true` is
  mandatory and asserted by `flux-bootstrap.yml`.
- **SOPS/age only where substitution can't go** (whole blocks/lists, or values
  needed at kustomize-*build* time). Not used yet.

### 3.7 GitOps — Flux

[ADR-0008](decisions/0008-flux-via-flux-operator.md),
[ADR-0028](decisions/0028-gitops-delivery-signed-oci-syncless-fluxinstance.md),
[ADR-0016](decisions/0016-calico-ansible-primes-flux-adopts.md).

- **Flux `2.9.x`**, installed by the **Flux Operator** (chart `0.58.0`), which
  is helm-installed by Ansible as the last provisioning step. The operator pins
  and auto-upgrades Flux within the minor and applies the strict-substitution
  patch.
- **The source is a cosign-signed OCI artifact, not the Git branch.** CI
  (`.github/workflows/gitops-artifact.yml`) builds `gitops/` into
  `oci://ghcr.io/nighlabs/homelab-infra/gitops` and signs it keyless via the
  GitHub Actions OIDC identity. An `OCIRepository` with `spec.verify` +
  `matchOIDCIdentity` refuses unsigned or forged artifacts. **Pushing to `main`
  reaches the cluster only after CI has signed it.**
- **The `FluxInstance` is sync-less** (`spec.sync` can't express `verify`).
  Ansible seeds the `OCIRepository` + root `Kustomization`, both committed
  *inside* the path the root reconciles (`gitops/deployment/<cluster>/`), so
  Flux adopts and then drift-corrects them.
- **Layout — four tiers**, reconciled in `dependsOn` order (see `gitops/CLAUDE.md`):

  ```
  gitops/deployment/<cluster>/  Flux entrypoints: source.yaml, sync.yaml, crds/infrastructure/apps
  gitops/crds/                  CRDs that must be Established before controllers (Calico's, vendored)
  gitops/infrastructure/        controllers: calico, calico-bgp, then cert-manager, ceph-csi, ESO, …
  gitops/apps/                  workloads (empty until the infra layer is up)
  ```
  The artifact root is `gitops/` itself, so paths inside are artifact-relative
  (`./infrastructure`, not `./gitops/infrastructure`).
- **"Ansible primes, Flux adopts."** Flux's own pods need a CNI, but the CNI is
  Flux-managed — so Ansible installs Calico once from the *same committed
  definition* (one `values.yaml`, one vendored CRD file, the same BGP
  manifests), and Flux's first reconcile is an adoption with no diff. The
  dual-applied set is deliberately small: Calico's values, the CRDs, the BGP CRs,
  the #12890 workaround, and the Flux root. Everything else is Flux-only.

### 3.8 Workloads (the non-GPU tier)

*Status: none deployed yet.* Each is a normal Deployment/StatefulSet, delivered
by Flux from `gitops/apps/`.

| Component | Role | Notes |
|---|---|---|
| **LiteLLM gateway** | One OpenAI-compatible endpoint for the whole stack | Routes by model name to the Mac's llama-swap **and** cloud providers; virtual keys, spend tracking, fallbacks |
| **Postgres** | LiteLLM's backing store (+ app data) | StatefulSet + `ceph-rbd` PVC |
| **Redis** | Cache, queues, rate-limit state | |
| **Qdrant** (or pgvector) | Vector store for RAG | StatefulSet + `ceph-rbd` PVC |
| **RAG / agent orchestrator** | App logic | Calls LiteLLM, not the Mac directly |
| **Open WebUI** | Chat front-end | Points at LiteLLM |
| **OTel Collector / Alloy** | Ships metrics + logs to a managed backend | Out-of-band; OTLP-swappable (§8) |
| **Gateway + cert-manager** | TLS, routing | §4.5–4.6 |

**LiteLLM is a router, not a loader.** It dispatches requests; the actual
load/unload of weights is llama-swap's job on the Mac. LiteLLM's `/model/new`
only edits the routing table — it never frees Mac memory.

```yaml
model_list:
  - model_name: agent
    litellm_params:
      model: openai/agent                       # name llama-swap serves
      api_base: http://mac-studio.lan:8080/v1
      api_key: "dummy"
  - model_name: embed
    litellm_params:
      model: openai/embed
      api_base: http://mac-studio.lan:8080/v1
      api_key: "dummy"
  - model_name: gpt-cloud                        # mix in cloud freely
    litellm_params:
      model: anthropic/claude-...
```

---

## 4. Networking

### 4.1 Networks, by role

Real subnets, VLAN tags, bridge names and addresses are **not in Git** — they
live in BWS and reach the repo only as `{{ bws.* }}` references and `${var}`
placeholders. By role:

| Network | Carries | Where |
|---|---|---|
| **DMZ / k3s cluster network** | node `eth0`: SSH, k3s API, pod and service traffic, BGP peering with pfSense | a dedicated VLAN on the 1Gb bond |
| **Ceph public network** | node `eth1`: ceph-csi → mons + all client I/O. The **only** Ceph network a k3s node ever touches | a dedicated VLAN on the jumbo (`mtu 8996`) bond |
| **Ceph cluster network** | OSD-to-OSD replication only, never client-facing | **untagged/native on that same jumbo bond** — so an untagged `eth1` silently lands on replication traffic; the VLAN tag is what keeps clients on the right one |
| **Management** | general/management only; Ceph doesn't use it | a separate 1Gb bond |
| **LoadBalancer range** | Calico-assigned Service IPs, one `/24` per cluster | **routed-only** — attached to no interface anywhere, learned by pfSense over BGP |

`eth1` has no default route and MTU 8996 set explicitly at both the Proxmox
NIC and the networkd unit (a silent fallback to 1500 defeats the point).

### 4.2 Address plan

[ADR-0011](decisions/0011-cluster-cidrs-never-cgnat.md),
[ADR-0026](decisions/0026-per-cluster-derivation-from-index.md).

- **Never CGNAT (`100.64.0.0/10`)** for any cluster CIDR — Tailscale routes the
  whole `/10` and Cloudflare reserves chunks of it.
- **Cluster CIDRs in `10.0.0.0/8`**, pinned explicitly: pods `10.42.0.0/16`,
  services `10.43.0.0/16`. Calico's IPPool must equal `cluster-cidr`
  (asserted at bootstrap).
- **Everything host-shaped derives from one number.** A node declares
  `node_number`; its DMZ IP, Ceph IP, both MACs and its vmid fall out of it. A
  cluster declares `index`; its ASN (`64600 + index`) and LB range
  (`<lb_range_base>.<index>.0/24`) fall out of that. `node_number` and hostname
  uniqueness is global across clusters (shared subnets, shared vmid space).

### 4.3 CNI and dataplane — Calico, eBPF, BGP-routed

[ADR-0010](decisions/0010-calico-over-cilium.md),
[ADR-0018](decisions/0018-calico-bgp-replaces-metallb.md),
[ADR-0024](decisions/0024-calico-ebpf-dataplane-no-kube-proxy.md),
[ADR-0019](decisions/0019-k3s-1.36-calico-3.32.1-version-pair.md).

- **Calico `v3.32.1`** via the tigera-operator chart, configured from one
  `values.yaml` (`gitops/infrastructure/calico/`).
- **eBPF dataplane (`linuxDataplane: BPF`), kube-proxy removed.** The reason is
  **source-IP preservation** under `externalTrafficPolicy: Cluster`, not
  throughput. DSR is deliberately off (default `Tunnel` mode).
- **BGP is the pod dataplane** (`bgp: Enabled`, `encapsulation: None`). All
  nodes share the DMZ subnet, so Calico's default node-to-node mesh (iBGP,
  one ASN per cluster) distributes pod CIDRs with no `BGPPeer` needed. The pfSense
  peer exists only for LoadBalancer advertisement.
- `natOutgoing: Enabled` — pod egress is SNAT'd to the node IP because nothing
  outside has a route back to the pod CIDR. That is route hygiene, not
  enforcement; real isolation is Calico `GlobalNetworkPolicy` /
  `ClusterNetworkPolicy` (never the deprecated AdminNetworkPolicy path).
- Node address autodetection is pinned to `NodeInternalIP` (= `eth0`), so
  Calico can never land on the Ceph NIC and no subnet appears in Git.
- Calico's CRDs are vendored under `gitops/crds/` and server-side applied,
  because v3.32 moved them out of the chart and three exceed the client-side
  apply limit ([ADR-0020](decisions/0020-crd-tier-vendored-server-side-apply.md)).

### 4.4 LoadBalancer IPs — Calico LB IPAM + BGP to pfSense

[ADR-0018](decisions/0018-calico-bgp-replaces-metallb.md),
[ADR-0022](decisions/0022-pfsense-frr-raw-config-explicit-neighbors.md),
[ADR-0023](decisions/0023-rfc8212-real-policy-le32.md). **No MetalLB.**

- **Allocation:** an `IPPool` with `allowedUses: [LoadBalancer]` over the
  cluster's `/24`; Calico's kube-controllers assigns every LoadBalancer Service
  an address (`assignIPs: AllServices` — revisit if a second LB IPAM provider
  is ever added). **Calico 3.32 ships a broken RBAC grant for this
  ([#12890](https://github.com/projectcalico/calico/issues/12890)); a workaround
  ClusterRole is mandatory** or IPs sit `pending` forever while BGP looks
  healthy. It's Ansible-primed and Flux-managed, with removal criteria.
- **Advertisement:** `BGPConfiguration.spec.serviceLoadBalancerIPs` + one global
  `BGPPeer` to pfSense (eBGP: cluster ASN `64601` ↔ pfSense `64512`). A
  `BGPFilter` exports **only the LB range** with an explicit catch-all `Reject`
  (Calico's default for unmatched routes is *Accept*), so pfSense never learns
  the pod CIDR.
- **pfSense/FRR side:** generated raw config with explicit `neighbor` lines
  per node in a per-cluster peer group; RFC 8212 satisfied by real prefix lists
  (`<CLUSTER>-IN permit <lb_range> le 32`, `-OUT deny any`) rather than
  disabled; `maximum-paths 8` for ECMP across nodes; `timers bgp 3 9` (BFD
  isn't available — open-source Calico doesn't implement it). The inbound prefix
  list is an **independent** control: the `BGPFilter` is enforced by the device
  we'd be guarding against misconfiguring.
- **`le 32` is load-bearing:** `Cluster`-policy Services advertise the whole
  block, `Local` ones a `/32` each; without `le 32` the latter establish a
  healthy session and blackhole.
- **Session state proves nothing.** RFC 8212 refusal and a missing `le 32`
  both look like `Established` with nothing flowing. The test is allocation
  (`EXTERNAL-IP` leaves `pending`) then reachability from another segment.
- Firewall: BGP (TCP/179) from an alias of node addresses to the firewall
  itself; the LB **supernet** sits in the "internal networks" alias so LB
  reachability fails closed. Runbook: [`pfsense-frr-bgp-setup.md`](pfsense-frr-bgp-setup.md).

### 4.5 Ingress / Gateway

[ADR-0013](decisions/0013-ingress-certs-dns-external-access.md).
**Gateway API via NGINX Gateway Fabric** (`gitops/infrastructure/nginx-gateway-fabric/`)
— cert-manager and external-dns both speak Gateway API. One shared `Gateway`
(namespace `nginx-gateway`, routes attach from any namespace); NGF 2.x
provisions its data plane **per Gateway**, so that object is what creates the
nginx Deployment + LoadBalancer Service. The Service gets its IP from Calico
(§4.4) and uses `externalTrafficPolicy: Cluster` — under eBPF both policies
preserve the source IP; `Cluster` also balances across all endpoints, while
`Local` balances per-node via ECMP `/32`s, so `Local` is only for a Service
that genuinely needs traffic pinned to backend-bearing nodes. The Gateway API
CRDs (standard channel) are vendored in `gitops/crds/gateway-api/` — they
belong to no chart, and `httproutes` exceeds the client-side apply limit
([ADR-0020](decisions/0020-crd-tier-vendored-server-side-apply.md)'s rule).
The HTTP listener serves today; the HTTPS listener arrives with the wildcard
cert (§4.6).

### 4.6 Certificates

*Status: next milestone.* **cert-manager + ACME via Cloudflare DNS-01.** DNS-01
yields valid public certs with **no inbound**, so even internal-only services
get real certs. A **wildcard** serves the internal endpoint too (§4.8).

### 4.7 Source-IP preservation

- **Internal / direct path:** preserved by the eBPF dataplane under
  `externalTrafficPolicy: Cluster` — verified: the pod sees the real
  off-cluster client, not the node ([ADR-0024](decisions/0024-calico-ebpf-dataplane-no-kube-proxy.md)).
- **Cloudflare path:** L4 preservation is impossible (cloudflared is the origin
  peer). Recover the client IP from `CF-Connecting-IP` / `X-Forwarded-For` via
  NGF's `NginxProxy` `RewriteClientIP` (`mode: XForwardedFor`,
  `trustedAddresses` = the cloudflared source, `setIPRecursively: true`),
  trusting **only** cloudflared.
- **Tailscale:** subnet routers SNAT by default — `--snat-subnet-routes=false`
  plus a return route to `100.64.0.0/10` to keep the tailnet client IP.

### 4.8 Split-horizon DNS

*Status: designed; internal-resolver approach still open.* Routing *all* local
traffic through Cloudflare Tunnel would make LAN access WAN-dependent and
reintroduce NAT reflection; split DNS avoids both.

- **Public view:** Cloudflare Tunnel (external-dns) for off-tailnet clients.
- **Internal view:** an internal resolver returns the Gateway's LB IP for the
  same hostnames — LAN traffic stays local.
- **Tailnet view:** Tailscale split DNS points the zone at the internal resolver.
- **Certs just work:** the DNS-01 wildcard is valid on every path.
- **pfSense gotcha:** Unbound's DNS-rebinding protection blocks private answers
  for public domains — whitelist the domain.
- **Internal-resolver options (open):** pfSense Unbound host overrides
  (simplest); a single internal wildcard `*.apps.<domain>` → Gateway IP with
  HTTPRoute host routing (lowest maintenance); a second external-dns instance
  with an internal provider (most GitOps-native).
- Only *local* access survives a WAN outage; remote access during your own WAN
  outage is unsolvable.

### 4.9 External access

*Status: designed.* **Cloudflare Tunnel (public) + Tailscale (private).**
Cloudflare Tunnel for zero-port-forward public exposure (outbound-only →
internal Gateway), with Cloudflare Access on sensitive routes. Tailscale is the
**human/remote-access layer**, not an inter-tier data path, and the candidate to
replace the separate WireGuard.

### 4.10 Inter-tier hop, Mac access & security

- **LiteLLM → Mac stays on the LAN — not Tailscale.** One switch-hop apart;
  LiteLLM reaches the Mac at a stable LAN name. The Mac sits on its own VLAN
  with a firewall rule allowing only the cluster to reach `:8080`. If the Mac
  ever leaves the LAN, repoint `api_base` at the tailnet name — a one-line
  change bought later.
- **Do not** expose the Mac's llama-swap endpoint publicly. Public exposure (if
  any) is the LiteLLM gateway or Open WebUI, behind TLS + auth.
- **LiteLLM holds the real auth boundary:** per-client/per-agent virtual keys
  with budgets; the Mac endpoint stays a dumb private backend.

---

## 5. Memory budget (the 256 GB)

A rough resident-set plan — adjust to the actual models:

| Item | Approx |
|---|---|
| macOS + background (after trimming) | ~12–20 GB |
| Embedding + reranker (warm) | ~2–4 GB |
| Agent/chat model, 32B 4-bit (warm) | ~20 GB |
| 70B 4-bit (warm or swappable) | ~40 GB |
| KV cache / batching headroom under load | grows with concurrency & context |
| Swappable experiment slot | whatever you pull |

Set `iogpu.wired_limit_mb` so the **sum of resident weights + KV cache** stays
comfortably under it, with the OS reserve outside it. KV cache under continuous
batching grows with concurrency and context length, so leave slack.

---

## 6. Stability notes / gotchas

- **Metal under sustained load** can be flaky on some macOS point releases. Pin
  macOS and tool versions, soak-test under realistic concurrency, and keep
  llama-swap's `KeepAlive` on so a crashed model process restarts. If end-to-end
  tests later fail intermittently, check this before assuming a cluster bug.
- **Cold starts:** a multi-GB model can take tens of seconds to load and
  health-check; hence the generous `healthCheckTimeout` and no TTL on the warm
  core.
- **Version churn:** llama-swap and vllm-mlx both ship fast. Pin versions and
  re-validate the config schema on upgrade.
- **Don't over-wire memory:** if the box beachballs or locks, lower
  `iogpu.wired_limit_mb`. The OS reserve is not optional.

---

## 7. Bring-up order — built and remaining

Each step has a clear pass/fail, so you always know which layer broke. Ansible
rebuilds the VMs + node bootstrap; Flux repopulates the cluster from the signed
artifact; Ceph PVs + backups will hold the data — together that's full
rebuildability. Evidence for every ✅ is in [`worklog.md`](worklog.md).

**Cluster tier**

1. ✅ **Flatcar VM shell** — two NICs, static addressing, data disk, key-only
   SSH; survives unattended reboot and from-scratch rebuild.
2. ✅ **k3s all-in-one server** via the sysext, baked into Ignition; unattended
   patch updates proven.
3. ✅ **pfSense/FRR peering** — generated raw config, prefix lists, firewall
   rules, parked ahead of the cluster.
4. ✅ **Calico** primed by Ansible: v3.32.1, eBPF, BGP no-encap, LB IPAM +
   #12890 workaround, BGP session `Established`, LoadBalancer IPs allocated and
   reachable from another segment.
5. ✅ **Flux** bootstrapped: operator + sync-less FluxInstance, signed OCI
   source verified (`SourceVerified=True`), all tiers Ready, Calico adopted
   without a diff war. The whole chain verified on one from-scratch `site.yml`.
6. **From Git, in dependency order:** ✅ NGINX Gateway Fabric — the first
   Flux-only delivery: Gateway API CRD tier + NGF 2.6.7 + the shared Gateway;
   LB IP allocated and reachable cross-segment, source IP preserved under
   `Cluster`. Then ⬜ cert-manager (DNS-01 wildcard) → ceph-csi-operator +
   StorageClasses → External Secrets Operator + Bitwarden SDK Server →
   Postgres + Redis → LiteLLM → confirm a chat completion routes end-to-end
   to the Mac.
7. ⬜ **Then:** Qdrant → RAG/orchestrator → Open WebUI → OTel Collector.
8. ⬜ **Split DNS + access:** internal resolver, Tailscale split DNS,
   Cloudflare Tunnel → Gateway; verify source-IP preservation on both paths.
9. ⬜ **Add the CP taint and provision workers** (1 CP + 3 workers). Node 2's
   join is a **dataplane event** — the first moment the BGP mesh carries real
   traffic, and mixed eBPF/iptables nodes are unsupported.
10. ⬜ **Ceph:** dedicated k8s pool + restricted client user on the Proxmox Ceph
    (can happen in parallel; doesn't block anything above ceph-csi).

**Mac tier**

11. ⬜ Server-ize macOS (§2.1), set + persist `iogpu.wired_limit_mb`, reboot
    and verify it sticks.
12. ⬜ Install vllm-mlx + llama-swap, write `llama-swap.yaml`, wrap in the
    LaunchAgent, reboot and confirm `:8080` comes back unattended. Confirm the
    cluster can reach it on the LAN before wiring LiteLLM.

---

## 8. Platform completeness

Operational layers beyond the core design. Decided; implementation pending.

- **Observability — managed backend, shipped via a vendor-neutral collector**
  ([ADR-0014](decisions/0014-observability-managed-backend.md)). In-cluster only
  a lightweight OpenTelemetry Collector (or Grafana Alloy) scraping Prometheus
  endpoints + the Mac over the LAN; storage, dashboards and alerting live
  **out-of-band** in a managed backend (New Relic free tier, or Grafana Cloud),
  so telemetry survives a cluster outage. Stand it up day 1, not during an
  incident.
- **Backups — NAS as the S3 target** ([ADR-0015](decisions/0015-backups-nas-s3-and-break-glass.md)):
  Velero (CSI snapshots + Kopia) for PVCs + resources; CNPG native backup
  (Barman → S3) for Postgres with PITR; Qdrant snapshot API. Schedule a periodic
  **restore drill**. Crown jewels (Bitwarden recovery + periodic encrypted SM
  export, an offsite copy of the repo, the data backups) go to offline backup.
- **Dependency currency — Renovate.** First-class Flux support for
  HelmRelease/OCIRepository/image tags; a regex manager tracks the pinned
  Flatcar image, k3s sysext, Calico, flux-operator, vllm-mlx and llama-swap in
  the Ansible vars.
- **Tooling boundaries:** Ansible provisions the **VMs** and configures the
  **Mac**; Ignition configures the **Flatcar nodes** (Ansible only generates and
  delivers it, never converges a running node); Flux delivers **cluster
  contents**. Three tools, one job each.
- **Out of scope (next layer up):** the application itself — agents, RAG
  ingestion/embedding/chunking, model selection/versioning, quality evals. This
  document is the *platform*.

## 9. Open decisions

Tracked with status **Open** or **Proposed** in the
[decisions index](decisions/README.md):

- Internal-resolver approach for split DNS (§4.8).
- Flatcar OS update policy — default auto-reboot vs a k8s-aware drain
  ([ADR-0030](decisions/0030-flatcar-os-update-policy.md)).
- Dropping Helm for Calico in favour of a manifest install
  ([ADR-0029](decisions/0029-drop-helm-for-calico.md)).
- Rendering Calico's CRDs at OCI build time instead of vendoring 3 MB in Git
  ([ADR-0020](decisions/0020-crd-tier-vendored-server-side-apply.md)).
- Control-node kubeconfig hygiene (the cluster-admin cert that
  `bootstrap-cluster.yml` leaves at `ansible/.kube/`) — see `ansible/CLAUDE.md`.
- Tests still owed: ceph-csi version vs Proxmox Ceph + kernel features on a real
  PVC; the actual API blip during a Proxmox HA restart of the CP VM.
