# Self-Hosted Inference Stack: Mac Studio + k8s Cluster
 
A two-tier design that keeps everything GPU-bound native on the Mac Studio (the only place it gets Metal) and pushes everything else onto a Kubernetes cluster (Flatcar + k3s) running as VMs on your existing Proxmox HA cluster. The two tiers talk over a private network using OpenAI-compatible HTTP.
 
---
 
## 1. Topology
 
```
clients / agents
      │
      ▼
┌─ Kubernetes tier — Flatcar + k3s, VMs on Proxmox HA (no GPU) ─
│  1 control-plane VM  (tainted · SQLite/kine · no etcd)  +  3 workers
│  LiteLLM gateway · Postgres · Redis · Qdrant · RAG/agent · Open WebUI
│  OTel Collector / Alloy → managed observability (out-of-band)
│  storage : ceph-csi → existing Proxmox Ceph   (RBD + CephFS)
│  provisioning : Ansible (VMs + Mac)   GitOps : FluxCD
└─ via LAN — segmented VLAN, firewall scopes :8080 to the cluster ─
      │
      ▼
┌─ Mac Studio (256 GB) — native, Metal ─────────────────────
│  llama-swap  (one stable endpoint :8080)
│    warm group (swap:false, no TTL) : agent · embedding · reranker
│    experiments (swap:true, TTL)    : models you're trying out
│  backend per model : vllm-mlx (continuous batching on Metal)
└────────────────────────────────────────────────────────────
```
 
**Why this split:** Metal can't be passed through to a container or VM, so inference must run natively on the host. Everything that *isn't* inference (routing, storage, app code, UI, observability) has no such constraint and belongs in k8s where it's reproducible and isolated. The two tiers meet at a single HTTP boundary.
 
---
 
## 2. Tier 1 — Mac Studio inference node
 
### 2.1 Turn macOS into a server
 
macOS has no official "server mode" switch, but four layers make it behave like one. The acid test: an unattended `sudo reboot` that comes back fully reachable with no monitor, keyboard, or person present.
 
**Never sleep**
```bash
sudo pmset -a sleep 0 displaysleep 0 disablesleep 1 powernap 0 womp 1
# womp 1 = wake on network; powernap 0 = no background wake churn
```
 
**Auto-login** (so a GUI session exists on boot — Metal is most reliable with an active WindowServer session)
- System Settings → Users & Groups → set automatic login to your service user.
- Caveat: **FileVault blocks auto-login.** Decide deliberately:
  - Disable FileVault → unattended reboot after a power cut works.
  - Keep FileVault → you must unlock manually after any cold boot.
- For an inference box on a private network, most people disable FileVault here. If the data on disk matters, keep it and accept manual unlock.
**Remote management**
```bash
sudo systemsetup -setremotelogin on        # SSH
# Enable Screen Sharing in Settings → General → Sharing for the rare GUI task
```
 
**Trim background services** (each one frees unified memory and stops disk/CPU churn)
- Disable Spotlight indexing — a headless server doesn't need it:
  ```bash
  sudo mdutil -a -i off
  ```
- Turn off: Siri, iCloud sync, Photos analysis, Time Machine (or at least local snapshots), Handoff, Notifications.
- Settings → Accessibility → Display → Reduce Motion + Reduce Transparency.
- Strip Login Items down to nothing.
- Consider a dedicated, low-privilege macOS user that owns the whole stack.
**Run the stack under launchd**, not a terminal you leave open.
- Because Metal wants the logged-in session, run inference as a **LaunchAgent** in the auto-login user's session (not a system LaunchDaemon). The `iogpu` sysctl below *is* a good fit for a LaunchDaemon since it's a kernel tunable.
Example LaunchAgent (`~/Library/LaunchAgents/local.llama-swap.plist`):
```xml
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
 
By default macOS lets the GPU wire ~75% of unified memory on machines above 36 GB — roughly **192 GB** of your 256 GB. Raise it so big models and several resident models fit, but leave real headroom for the OS and the non-wired parts of each model process.
 
```bash
# 224 GB to the GPU, ~32 GB left for macOS + overhead. Tune to taste.
sudo sysctl iogpu.wired_limit_mb=229376
 
# verify
sysctl iogpu.wired_limit_mb
```
 
Persist it (resets to default `0` on reboot otherwise). Either `/etc/sysctl.conf`:
```bash
echo "iogpu.wired_limit_mb=229376" | sudo tee -a /etc/sysctl.conf
```
…or a LaunchDaemon running the `sysctl` command at load. Don't go to 100% — leaving too little for the OS causes beachballs, hard locks, or a reset. Start conservative (32 GB free) and tighten only if you confirm stability under load.
 
### 2.3 The inference layer: vllm-mlx behind llama-swap
 
- **vllm-mlx** (or the official **vllm-metal** plugin) is the per-model engine: continuous batching + paged KV cache on Metal, OpenAI-compatible. This is what scales under concurrent agents/RAG, unlike Ollama's sequential queue.
- **llama-swap** sits in front as a single stable endpoint and manages process lifecycle: it starts a model's backend on first request, health-checks it, and unloads idle models after a TTL.
Design the model set as **warm core + swappable experiments**:
 
```yaml
# llama-swap.yaml  — field names are illustrative; verify against your
# pinned llama-swap version, the schema moves fast.
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
 
Key principle from earlier: **never swap your core.** The agent/chat model, the embedding model, and a reranker live in the `warm` group with `ttl: 0` so retrieval and agent loops never pay a cold-start. Everything you're just trying out goes in the `experiments` group with a TTL so it frees memory on its own. With 256 GB you have room to keep a 70B-class model plus the embedding/rerank pair warm and still swap experiments underneath.
 
### 2.4 Storage
 
- Keep all weights on the Mac's local SSD (fast load). Point everything at **one** Hugging Face cache so you don't duplicate multi-GB files:
  ```bash
  export HF_HOME=/Users/svc/models   # set in the LaunchAgent env too
  ```
- This single-cache discipline matters more than the RAM suggests — disk fills faster than memory here.
---
 
## 3. Tier 2 — Kubernetes cluster (everything non-GPU)
 
### 3.1 Cluster shape
 
- **Host layer:** 4 VMs on your existing 3-host Proxmox HA cluster. All four VMs run under Proxmox HA on shared/replicated Ceph, so a host failure restarts the VM elsewhere.
- **Node layout:** **1 dedicated control-plane node + 3 workers.** The control-plane VM is small (2 vCPU / 4 GB), **tainted** so no workloads land on it. Workers are sized for the workload set (RAM-heavy for Qdrant/Postgres).
- **OS:** Flatcar Container Linux. Install k3s via the **K3s sysext** (from `flatcar/sysext-bakery`) — immutable, updated by systemd-sysupdate, Renovate-trackable — rather than dropping the binary in `/opt/bin`. Provisioned by **Ignition** (no PXE, no Ignition server), delivered via the **cloud-init config drive** — the per-node Ignition is set as the cloud-init *user-data* with `--cicustom "user=<storage>:snippets/<node>.ign"`, which the proxmoxve image's default OEM reads as its Ignition source. *Chosen over the fw_cfg/`args` method because that requires root@pam (Proxmox locks `args`), at odds with scoped-token automation — see Appendix A.* Because Flatcar can't consume Ignition **and** regular cloud-init on the same channel, the Proxmox cloud-init GUI fields go inert and **all** node identity/keys/networking live in the Ignition (DHCP sidesteps the fragile config-drive network-data path). Still **validate delivery on a single hand-built node** before generalizing into the Ansible loop (see §3.4 and Bring-up). Coordinate auto-updates with the **Flatcar Update Operator (FLUO)** so node reboots cordon/drain first.
- **Distribution:** **k3s** (chosen over RKE2/Talos for single-binary simplicity and the SQLite datastore).
- **Datastore:** embedded **SQLite via kine** — no etcd. Single control-plane node; availability comes from the Proxmox layer, not an etcd quorum.
**k3s server config** — baked into Ignition as `/etc/rancher/k3s/config.yaml`:
```yaml
# CNI/policy: Calico owns both. disable-network-policy only turns off k3s's
# built-in kube-router controller — NetworkPolicies still work (enforced by Calico).
flannel-backend: none
disable-network-policy: true
 
disable:                  # drop bundled components we replace
  - traefik               #   → NGINX Gateway Fabric
  - servicelb             #   → MetalLB
  - local-storage         #   → ceph-csi
disable-helm-controller: true   # Flux owns Helm
 
cluster-cidr: 10.42.0.0/16      # k3s defaults, pinned explicitly (§4.1);
service-cidr: 10.43.0.0/16      #   Calico IPPool must match cluster-cidr
 
secrets-encryption: true
node-taint:                     # control-plane node only (workers omit this)
  - "node-role.kubernetes.io/control-plane=true:NoSchedule"
tls-san:
  - "<stable-api-name>"         # so the cert is valid via the stable name
```
Keep **CoreDNS** (required) and **metrics-server** (HPA + `kubectl top`). Keep **kube-proxy** (revisit only if you move Calico to eBPF). Agents get the minimal join config (server URL + token), not the flags above.
 
### 3.2 Availability & datastore durability
 
- **Control-plane HA = Proxmox HA**, not k8s multi-master. If the host dies, Proxmox restarts the CP VM on a surviving host; workloads on the agents keep running through the brief blip.
- **Upgrades:** snapshot the CP VM → upgrade k3s → verify (or roll the snapshot back). A single-server k3s upgrade is a sub-minute *control-plane* blip, not cluster downtime — workloads never stop.
- **Datastore durability:** the SQLite DB lives on replicated Ceph (survives host failure). Optionally add **Litestream** for continuous offsite, point-in-time backup of the datastore, independent of storage replication.
- *Not* dqlite/rqlite: kine needs a single writer with a strictly +1 revision (multi-master breaks it), and dqlite is deprecated in k3s anyway.
### 3.3 Persistent storage — ceph-csi against the existing Proxmox Ceph
 
- Reuse the **Proxmox Ceph** cluster; do **not** run a second Ceph inside k8s.
- Deploy via the **ceph-csi-operator** (the only supported ceph-csi deployment mode as of v3.16) pointed at the external cluster. (Rook-external also works but now sits on the same operator; ceph-csi-direct is leaner when Proxmox owns Ceph's lifecycle.)
- Two StorageClasses: **`ceph-rbd`** (RWO block) for Postgres/Qdrant/Redis; **`cephfs`** (RWX) for anything needing shared file access.
- RBD volumes aren't node-bound, so a dead worker → k8s reschedules the pod and re-attaches the volume on a healthy node. Enable Non-Graceful Node Shutdown (the `out-of-service` taint) so RBD detaches from a hard-failed node.
- **Setup checklist:** dedicated Ceph **pool + restricted client user** for k8s (don't touch PVE's VM pool); second **vNIC on the Ceph VLAN** per node so the CSI client reaches the mons/OSDs; **match ceph-csi version** to the Proxmox Ceph release; watch RBD/CephFS image features vs the Flatcar kernel.
- **Upgrade order:** confirm version overlap → upgrade the Proxmox Ceph cluster (mons → mgr → OSDs, verify, finalize with `require-osd-release`) → upgrade ceph-csi via the operator → test a PVC. Clients tolerate skew within the supported window.
### 3.4 Provisioning — Ansible
 
Ansible owns VM provisioning (and the Mac, §8). **No Terraform** — see Appendix A for the reversal and reasoning (state-file secret handling, one fewer tool, drift detection not needed at this scale; consistent rebuild preserved via playbook + Flux + Ceph).
 
- **VM lifecycle:** a loop over a node map (role, cores, mem) using the `community.general.proxmox*` modules — clone the Flatcar template per node, set specs, attach the per-node Ignition.
- **Template build folded in:** the same Ansible repo builds the Flatcar template (download the proxmoxve image → import → convert to template) as an idempotent role, replacing what would otherwise be a standalone `make_template.sh`.
- **Ignition generation:** per-node **Butane rendered with Jinja2** from the node map, transpiled with `butane --strict`, uploaded to a snippets-enabled storage, referenced by `--cicustom "user=..."` (delivery decision in §3.1 / Appendix A).
- **Auth:** dedicated scoped `ansible@pve` user + API token (the config-drive route needs **no root@pam** — that's only the fw_cfg/`args` path); SSH to the host for the snippet upload and any `qm` fallback tasks. ~~Secrets (PVE token, BW bootstrap token) live in **Ansible Vault**~~ — **SUPERSEDED 2026-08-17: secrets come from BWS at run time; there is no `vault.yml`. Secret zero (the BWS token) lives in the macOS Keychain. See Appendix A, "Control-node secrets".** Used transiently either way — never persisted to a state file.
- **cicustom wrinkle:** if `proxmox_kvm` doesn't expose `cicustom` in the pinned version, set it via the API (`uri`) or a `command` task running `qm set --cicustom` (delegated to the PVE host). `cicustom` is settable by a suitably-permissioned token; it does **not** trip the root@pam wall.
- **Rebuild:** re-run the playbook (after a GUI delete or an Ansible teardown play) and the cluster comes back identically; Flux then repopulates workloads. The source of truth is Proxmox queried live, not a state file that can skew, lock, or leak.
### 3.5 GitOps — FluxCD
 
- **Flux** (chosen over ArgoCD for its CRD-native, manifest-everything model and lighter footprint, with no Application/AppProject layer to manage).
- Installed via the **Flux Operator** (declarative `FluxInstance` CRD), **bootstrapped by Ansible** as the final provisioning step — the same playbook run that builds the VMs installs the Flux Operator and applies the `FluxInstance` pointed at your Git repo, then steps aside. The Git deploy key/token comes from **BWS** at run time (~~Ansible Vault~~ — superseded 2026-08-17, Appendix A); nothing secret is persisted to a state file.
- All workloads arrive via Flux **HelmReleases / Kustomizations** from Git, with `dependsOn` ordering (storage → databases → apps).
### 3.6 Secrets architecture
 
Two distinct layers — keep them separate:
 
- **Datastore secrets-at-rest — DECIDED.** k3s `--secrets-encryption` (aescbc) so k8s Secret objects are AES-encrypted in the SQLite datastore, plus full-disk encryption on the control-plane VM. Enable it at first server start; back up the encryption key off-cluster. A cloud-KMS-as-KEK upgrade is possible later, traded against a cold-start dependency on KMS reachability (the control plane needs the KMS to decrypt on reboot).
- **GitOps app secrets — DECIDED: External Secrets Operator + Bitwarden Secrets Manager.** ESO syncs secrets from Bitwarden SM into native K8s Secret objects, so Git holds only `ExternalSecret` *references* — no encrypted secret material, no immutable history. Pods restart offline because the materialized Secrets persist in the datastore; ESO is only needed at *sync* time, not pod-start. Bitwarden SM is zero-knowledge (the provider can't read your secrets) and logs every access, and its **free tier** (unlimited secrets, 3 projects, 3 machine accounts) covers this cluster. The ESO Bitwarden provider runs a small **Bitwarden SDK Server** in-cluster (cert via cert-manager).
  - *Auth / least privilege:* a dedicated Bitwarden **project** holds the infra secrets, accessed by a **machine-account token scoped read-only to that project** (never the personal vault), with an **expiry** set. That token is the single bootstrap "secret zero" — ~~seeded from **Ansible Vault**~~ **SUPERSEDED 2026-08-17: it lives in the macOS Keychain and is read at task time, so nothing secret remains in the repo directory. See Appendix A, "Control-node secrets".** Used transiently, never persisted to a state file.
  - *Supply-chain hygiene:* after the April 2026 `@bitwarden/cli` npm compromise (note: the SDK Server and the `bws` binary are separate from that npm package), pin the SDK Server **image by digest**, pin provider/`bws` versions and verify checksums, and keep the token scope tight so a compromised tool's blast radius is one project's secrets — then watch the access audit log.
  - *Anti-lock-in:* workload manifests reference only `ExternalSecret` CRDs, so the backing store is swappable — repoint a single `SecretStore` from Bitwarden to Vault/OpenBao or a cloud manager without touching workloads. (We dropped the SOPS+OCI alternative: Bitwarden's zero-knowledge model already gives the "cloud can't read plaintext" property, and ESO is fewer moving parts — no SOPS key, no OCI pipeline. Both paths end with plaintext K8s Secrets in-cluster anyway, protected by the aescbc layer above.)
### 3.7 Workloads (the non-GPU tier)
 
Each is a normal Deployment/StatefulSet, delivered by Flux — no GPU, fully reproducible.
 
| Component | Role | Notes |
|---|---|---|
| **LiteLLM gateway** | One OpenAI-compatible endpoint for the whole stack | Routes by model name to the Mac's llama-swap **and** any cloud providers; virtual keys, spend tracking, fallbacks |
| **Postgres** | LiteLLM's backing store (+ app data) | StatefulSet + `ceph-rbd` PVC; holds keys/budgets/logs |
| **Redis** | Cache, queues, rate-limit state | |
| **Qdrant** (or pgvector) | Vector store for RAG | StatefulSet + `ceph-rbd` PVC |
| **RAG / agent orchestrator** | Your app logic | Calls LiteLLM, not the Mac directly |
| **Open WebUI** | Chat front-end | Points at LiteLLM |
| **OTel Collector / Alloy** | Ships metrics + logs to a managed backend | New Relic free (lean) / Grafana Cloud free; out-of-band, OTLP-swappable |
| **Ingress + cert-manager** | TLS, routing | If you expose any of it |
 
**LiteLLM is a router, not a loader.** It dispatches requests; the actual load/unload of weights is llama-swap's job on the Mac. LiteLLM's `/model/new` only edits the routing table at runtime — it never frees Mac memory. So the lifecycle behavior lives in Tier 1; LiteLLM just gives you one clean endpoint over local + cloud.
 
Sketch of the relevant LiteLLM config:
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
      model: anthropic/claude-...                # example external route
```
 
---
 
## 4. Networking
 
### 4.1 Address plan (CIDRs)
 
- **The existing LAN's RFC1918 range is reserved** (real value in the Ansible vault) — LAN, Proxmox, and Ceph all live there. Don't reuse it for cluster-internal ranges.
- **Do NOT use CGNAT (`100.64.0.0/10`) for any cluster CIDR.** It's a trap in this stack: Tailscale installs a route for the whole `/10` and drops `100.64/10` traffic not arriving on `tailscale0`, and Cloudflare reserves `100.64/12`, `100.80/16`, `100.96/12`, `100.112/16`. The "CGNAT avoids RFC1918 conflicts" reasoning does **not** apply here — it actively collides with Tailscale and Cloudflare.
- **Cluster-internal CIDRs live in `10.0.0.0/8`** (free here), pinned explicitly (k3s defaults made non-implicit): `cluster-cidr 10.42.0.0/16`, `service-cidr 10.43.0.0/16`. **Calico's IPPool must match `cluster-cidr`.**
- **MetalLB service pool:** a dedicated **`/24` routed by pfSense** out of the reserved LAN range — *not* carved from node-VLAN host space (cleaner, and it's what BGP advertises). Exact subnet **TBD** (pending node-VLAN finalization).
### 4.2 CNI — Calico
 
Install k3s with `flannel-backend: none` + `disable-network-policy: true` (§3.1), then deploy **Calico** for the CNI + NetworkPolicy. (Chosen over Cilium: policy support without Cilium's eBPF setup friction.) Calico's IPPool must match `cluster-cidr` (`10.42.0.0/16`). Use NetworkPolicies to segment namespaces — closes the workload-isolation gap.
 
### 4.3 Load balancing — MetalLB in BGP mode
 
Gateway API / NGINX Gateway Fabric needs LoadBalancer IPs, and nothing in the original design provided them. **MetalLB** fills that gap, in **BGP mode peering with pfSense** (via its FRR package) — not L2.
 
- *Why BGP over L2:* a dedicated **routed** address range (cleaner than carving node-VLAN host space), **ECMP** spread plus faster failover across workers, and it pairs with `externalTrafficPolicy: Local` to preserve real client IPs while still distributing.
- *ASNs:* two private ASNs from `64512–65534` — one for pfSense, one for MetalLB.
- *No SPOF:* peer **all worker nodes**, not a single node.
- *FRR gotcha:* on current FRR you must enable **"Disable eBGP Require Policy"** (or add a route-map / prefix-list), or it silently refuses MetalLB's routes.
### 4.4 Ingress / Gateway
 
- **Gateway API via NGINX Gateway Fabric (NGF)** — future-proof over legacy Ingress; cert-manager and external-dns both speak Gateway API. The Gateway's LoadBalancer Service gets its IP from MetalLB (§4.3).
- k3s ships Traefik + servicelb; both are disabled in the k3s config (§3.1) in favor of NGF + MetalLB.
### 4.5 Certificates
 
**cert-manager + ACME via Cloudflare DNS-01.** DNS-01 (not HTTP-01) yields valid public certs with **no inbound** — pairs with the no-port-forward goal, and even internal-only services get real certs. Issue a **wildcard** so the same cert serves the internal endpoint too (see split DNS, §4.7).
 
### 4.6 Source-IP preservation (hard requirement)
 
- **Internal / direct path:** set `externalTrafficPolicy: Local` on the Gateway's LoadBalancer Service — preserves the real client IP at L4 (no SNAT). BGP + `Local` keeps the IP *and* spreads across nodes that have a ready gateway pod.
- **Cloudflare path:** L4 preservation is **impossible** — cloudflared forwards to the Gateway via ClusterIP, so cloudflared is the origin peer. Recover the real client IP from the **`CF-Connecting-IP` / `X-Forwarded-For`** header via NGF's `NginxProxy` **RewriteClientIP** (`mode: XForwardedFor`, `trustedAddresses` = the cloudflared source, `setIPRecursively: true`), trusting **only** cloudflared.
- **Tailscale wrinkle:** subnet routers SNAT by default — to preserve the real tailnet client IP set `--snat-subnet-routes=false` plus a return route to `100.64.0.0/10`.
### 4.7 Split-horizon DNS
 
Routing *all* local traffic through Cloudflare Tunnel makes local access WAN-dependent, undermining the local-first design. Split DNS fixes that and also eliminates the prior "NAT oddities" (which were NAT reflection / hairpinning — split DNS avoids NAT entirely).
 
- **Public view:** Cloudflare Tunnel (external-dns) for off-tailnet remote clients.
- **Internal view:** an internal resolver returns the Gateway's **MetalLB IP** for the same hostnames, so LAN traffic stays local — no WAN, no NAT reflection.
- **Tailnet view:** Tailscale split DNS (per-domain nameserver) points the zone at the internal resolver.
- **Certs just work:** DNS-01 **wildcard** certs mean the internal endpoint serves the same valid cert — no TLS mismatch, no internal CA.
- **pfSense gotcha:** Unbound's DNS-rebinding protection blocks private answers for public domains — **whitelist the domain**, or the internal overrides get stripped.
- **Internal-resolver options (pick one — TBD):** pfSense Unbound host overrides (simplest); a single internal wildcard `*.apps.<domain>` → Gateway IP with HTTPRoute host routing (lowest maintenance); or a second external-dns instance with an internal provider (most GitOps-native).
- *Caveat:* split DNS only preserves **local** access during a WAN outage; remote access during *your own* WAN outage is unsolvable.
### 4.8 External access
 
**Cloudflare Tunnel (public) + Tailscale (private).** Cloudflare Tunnel for public exposure with zero port-forwarding (outbound-only → internal Gateway); add Cloudflare Access for auth on sensitive routes (note: Cloudflare terminates public TLS, and L4 client-IP is lost — recover it per §4.6). Tailscale is the **human/remote-access layer** (your devices → the Mac and cluster), not an inter-tier data path, and the candidate to replace your separate WireGuard.
 
### 4.9 Inter-tier hop, Mac access & security
 
- **LiteLLM → Mac stays on the LAN — not Tailscale.** The two tiers are one switch-hop apart, so LiteLLM reaches the Mac at a stable LAN name (DHCP reservation or static IP). Segment the Mac onto its own VLAN with a firewall rule allowing only the cluster to reach `:8080`; optionally TLS on llama-swap. Simple, fast, no overlay dependency.
- **Migration path:** if the Mac ever leaves the LAN, put it on the tailnet and repoint LiteLLM's `api_base` at the tailnet name — a one-line change bought later, not paid for now.
- **Inside the rack**, the k8s node VMs sit on the Proxmox cluster network for k8s/pod traffic, plus a **second vNIC on the Ceph VLAN** so the ceph-csi client reaches the mons/OSDs directly. Keep Ceph on its own 10G+ VLAN.
- **Do not** expose the Mac's llama-swap endpoint publicly — keep it LAN/VLAN-scoped to the cluster. Public exposure (if any) is the LiteLLM gateway or Open WebUI, behind TLS + auth.
- **LiteLLM holds the real auth boundary:** per-client/per-agent **virtual keys** with budgets, so the Mac endpoint stays a dumb private backend.
---
 
## 5. Memory budget (the 256 GB)
 
A rough resident-set plan — adjust to your actual models:
 
| Item | Approx |
|---|---|
| macOS + background (after trimming) | ~12–20 GB |
| Embedding + reranker (warm) | ~2–4 GB |
| Agent/chat model, 32B 4-bit (warm) | ~20 GB |
| 70B 4-bit (warm or swappable) | ~40 GB |
| KV cache / batching headroom under load | grows with concurrency & context |
| Swappable experiment slot | whatever you pull |
 
Set `iogpu.wired_limit_mb` so the **sum of resident model weights + KV cache** stays comfortably under it, with the OS reserve outside it. KV cache under continuous batching grows with concurrency and context length, so leave slack rather than packing to the limit.
 
---
 
## 6. Stability notes / gotchas
 
- **Metal under sustained load** can be flaky on some macOS point releases — there are reports of Metal-framework crashes on Mac Studio under light/moderate load on specific OS versions. Pin your macOS and tool versions, soak-test under realistic concurrency before trusting it, and keep llama-swap's `KeepAlive` on so a crashed model process restarts.
- **Cold starts:** a multi-GB model can take tens of seconds to load and health-check; that's why `healthCheckTimeout` is generous and why warm-core models never get a TTL.
- **Version churn:** llama-swap and vllm-mlx both ship fast. Pin versions in your LaunchAgent/automation rather than tracking latest, and re-validate the config schema when you upgrade.
- **Don't over-wire memory:** if you push `iogpu.wired_limit_mb` too high and the box starts beachballing or locks, lower it. The OS reserve is not optional.
---
 
## 7. Bring-up order
 
**Mac tier**
1. Server-ize macOS (sleep, auto-login, SSH, trim services, Spotlight off); set + persist `iogpu.wired_limit_mb`; reboot and verify it sticks.
2. Install vllm-mlx + llama-swap; write `llama-swap.yaml`; wrap in the LaunchAgent; reboot and confirm `:8080` comes back unattended.
**Cluster tier**
3. **Network prep (pfSense):** finalize the node VLAN, carve the MetalLB `/24` out of the reserved LAN range, and configure FRR — two private ASNs (`64512–65534`), peer **all worker nodes**, and enable **"Disable eBGP Require Policy."** Whitelist the domain in Unbound (rebinding protection) so split DNS can return private answers.
4. **Ceph:** dedicated k8s pool + restricted client user on the Proxmox Ceph; confirm a node can reach the mons.
5. **Spike one node (de-risk Ignition):** hand-build a single Flatcar + k3s node to confirm **Ignition delivery via the cloud-init config drive** (`cicustom` user-data → proxmoxve OEM) and k3s bring-up with the `config.yaml` (sysext install, disabled components, pinned CIDRs, `secrets-encryption`). Don't generalize until this is solid.
6. **Wrap in Ansible:** turn the validated node into the Ansible role + loop over the node map; generalize to **1 CP + 3 workers**. (The template build is an Ansible role too — §3.4.)
7. **One `ansible-playbook` run:** the VMs come up, k3s self-bootstraps from Ignition, and the same play bootstraps the Flux Operator pointed at your Git repo.
8. **From Git, via Flux (dependency order):** Calico → MetalLB (BGP) → NGINX Gateway Fabric + cert-manager (DNS-01 wildcard) → ceph-csi-operator + StorageClasses → External Secrets Operator + Bitwarden SDK Server → Postgres + Redis → LiteLLM (→ Mac over the LAN) → confirm a chat completion routes end-to-end.
9. **From Git:** Qdrant → RAG/orchestrator → Open WebUI → OTel Collector / observability.
10. **Split DNS + access:** internal resolver returns the Gateway's MetalLB IP; point Tailscale split DNS at it; bring up the Cloudflare Tunnel → Gateway for the public view; verify source-IP preservation on both the direct (`externalTrafficPolicy: Local`) and Cloudflare (`RewriteClientIP`) paths.
 
**Connectivity:** confirm the cluster can reach the Mac on the LAN (`curl http://mac-studio.lan:8080`) before step 8 — a DHCP reservation/static IP plus the VLAN firewall rule scoping `:8080` to the cluster.
 
Each step has a clear pass/fail, so you always know which layer broke. Ansible rebuilds the VMs + node bootstrap; Flux repopulates the workloads from Git; Ceph PVs + backups hold the data — together that's full rebuildability.
 
## 8. Platform completeness — next-step decisions
 
Operational layers beyond the core design. Decided here; implementation pending.
 
**Networking — consolidated in §4.** CNI (Calico), the address plan + CGNAT trap, load balancing (MetalLB in BGP mode), ingress (NGINX Gateway Fabric), certificates (cert-manager DNS-01 wildcard), DNS (external-dns + split-horizon), source-IP preservation, and external access (Cloudflare Tunnel + Tailscale) are all designed there.
 
**Observability — managed backend, shipped via a vendor-neutral collector.** In-cluster you run only a lightweight **OpenTelemetry Collector (or Grafana Alloy)** — it scrapes the Prometheus endpoints (ServiceMonitors/PodMonitors + the Mac over the LAN), collects logs, and exports via **OTLP / Prometheus remote-write**. Storage, dashboards, and alerting live in a **managed backend**, so you're not hosting Prometheus/Loki/Grafana at all, and the backend is **out-of-band** — it survives a cluster outage, exactly when you need to see metrics.
- *Lean — New Relic free:* 100 GB/mo ingest, full platform, one free full user, native OTLP + Prometheus remote-write. Its **ingest-based** model sidesteps Grafana Cloud's 10k-active-series cap (which a default k8s stack blows past); a homelab's metric volume stays well under 100 GB — logs are the only real consumer.
- *Alternate — Grafana Cloud free:* 10k series / 50 GB logs / 14-day retention / 3 users; watch the series cap (scrape selectively / Adaptive Metrics); Pro is ~$19/mo + usage if outgrown.
- *Swappability:* because the collector speaks OTLP, the backend is repointable with a config change — same anti-lock-in lever as ESO. So pick on free-tier fit, not lock-in.
- *Mac tier — scraped by the in-cluster collector over the LAN:* vllm-mlx `/metrics` (KV-cache, queue depth, TTFT, e2e latency, tokens) + node_exporter (darwin) + a powermetrics-based exporter for Apple Silicon GPU/VRAM, run as LaunchAgents.
- *LLM layer (later):* Honeycomb (wide-event trace debugging) or Langfuse/Phoenix for traces, token usage, and evals on the agent/RAG pipeline.
- *Trade-off:* operational telemetry leaves the network. Self-host the LGTM stack only if on-prem-purity or long retention wins — at the cost of running it and it being cluster-fate-shared (down when you need it).
- Stand it up day 1, not during an incident.
**Dependency currency — Renovate.** First-class Flux support (HelmRelease/OCIRepository/image tags) → PRs for bumps. With provisioning and the Mac both in the Ansible repo, Renovate's regex manager tracks the pinned Flatcar image, K3s sysext, vllm-mlx, and llama-swap versions there too.
 
**Backups — NAS as the target, via S3.** Run MinIO (or native S3) on the existing NAS as the backup destination.
- *Cluster-wide:* Velero (CSI snapshots + Kopia) → NAS-S3 for PVCs + resources.
- *Postgres:* CNPG native backup (Barman → S3) → NAS-S3 — pushes out of Ceph with point-in-time (WAL) recovery. (Not CNPG→CephFS, which leaves it stuck in Ceph.)
- *Qdrant:* snapshot API → NAS.
- Existing NAS backup job covers retention/offsite. Schedule a periodic **restore drill** — untested backups aren't backups.
**Crown-jewels / break-glass.** Because ESO makes K8s Secrets projections of Bitwarden, most secrets regenerate (k3s tokens, GHCR PAT, BW machine token, aescbc key, TLS certs). Truly irreplaceable → offline backup: (1) **Bitwarden account recovery** (recovery code + 2FA) plus a periodic encrypted **export** of the SM secrets; (2) an **offsite copy of the Git repo** (and the Ansible repo + Vault key); (3) the **data backups** above.
 
**Automation — Ansible (decided; now owns provisioning too).** Ansible owns **both** the Mac and Proxmox VM provisioning. On the Mac — the one mutable, hand-configured host — it's the obvious fit: Homebrew installs, launchd plists, `sysctl`/`pmset`, vllm-mlx + llama-swap, and the Prometheus exporters as idempotent tasks. For the cluster it builds the Flatcar template, renders per-node Ignition (Jinja2 → `butane`), clones/configures the VMs, attaches Ignition via `cicustom`, and bootstraps Flux (§3.4). This closes the reproducibility gap on **both** tiers and makes pinned versions Renovate-trackable. Pair it with LiteLLM **fallbacks** to a cloud model so the cluster degrades gracefully when the Mac is unreachable.
- *Tooling boundaries (revised — Terraform dropped):* Ansible provisions the **VMs** and configures the **Mac**; Ignition configures the **Flatcar nodes** (immutable, no package manager — Ansible only *generates and delivers* the Ignition, it never converges the running node); Flux delivers **cluster contents**. Terraform/OpenTofu was the original VM-provisioning choice but was **dropped** — see Appendix A (state-file secret handling, a fourth tool, and drift detection we don't need at this scale; consistent rebuild is preserved via playbook + Flux + Ceph). Each tool still does one job — there are just three now, not four.
**Out of scope (next layer up).** The application itself — agents, RAG ingestion/embedding/chunking, model selection/versioning, quality evals. This document is the *platform*; the AI app runs on top of it.
 
**Open items / TBD.**
- **Node VLAN subnet** — finalizes the MetalLB `/24` and the BGP peer addresses (§4.1, §4.3).
- **Ignition delivery** — *decided: cloud-init config drive (`cicustom` user-data), not fw_cfg/`args`* (Appendix A). The single-node spike still confirms the proxmoxve OEM consumes the raw Ignition JSON cleanly before generalizing into the Ansible loop (§3.1, Bring-up step 5).
- **Internal-resolver approach** — pfSense Unbound overrides vs an internal wildcard + HTTPRoute host routing vs a second external-dns instance (§4.7).
---
 
 
 
## Appendix A — Decision log (alternatives considered)
 
What was chosen at each fork, what was rejected, and why — recorded here so the reasoning survives even when the original discussion doesn't. Read this when a choice looks arbitrary later.
 
**Inference engine → vllm-mlx + llama-swap.**
- *Ollama* rejected: it wraps llama.cpp, and even with `OLLAMA_NUM_PARALLEL` it queues rather than doing true continuous batching, so it doesn't scale under concurrent agents/RAG (fine single-stream, non-scaling beyond that). vllm-mlx brings continuous batching + paged KV to Metal.
- *Plain MLX server / LM Studio* rejected: MLX server concurrency is basic; LM Studio is GUI-oriented.
- *llama-swap* chosen as the front-end for load-on-demand/unload-idle across per-model vllm-mlx processes (warm-core group never evicted; experiments TTL'd).
**GPU + containers → native on the Mac.**
- *Containerized inference* rejected: macOS can't pass Metal through to a VM/container (Docker, Apple `container`, Podman all run in a VM with no Metal); the Vulkan-via-Podman shim is slow/fragile. Inference runs native; everything else is containerized and reaches it over HTTP.
**k8s distribution → k3s.**
- *RKE2* rejected: mandatory embedded etcd (the ~1 vCPU steady-state cost observed).
- *Talos* rejected: it *is* a k8s distro shipping its own upstream Kubernetes — you can't run k3s on it (category mismatch), and its no-shell appliance model wasn't the aim here.
**Datastore → SQLite via kine (no etcd).**
- *Embedded etcd HA* rejected: the footprint being avoided; only needed for multi-master.
- *External Postgres/MySQL datastore* rejected: gives etcd-free HA but the DB must live outside the cluster and be made reliable itself — extra moving part.
- *dqlite/rqlite* rejected: Raft underneath = the same consensus cost as etcd; kine doesn't support them and dqlite is deprecated in k3s. Multi-master SQLite replication also violates kine's strictly-+1 revision requirement.
**Control-plane HA → single CP node, HA via Proxmox.**
- The CP VM runs under Proxmox HA (restart on host failure) on replicated Ceph; upgrades use a VM snapshot + a sub-minute API blip while workloads keep running on agents.
- *3-server embedded etcd* rejected: reintroduces the etcd footprint.
- *Managed cloud control plane* (EKS Hybrid Nodes, etc.) rejected: no free tier, and it parks etcd — hence all Secrets — in the cloud, against the on-prem goal.
- *"Temporarily add a CP node for upgrades"* rejected: impossible from SQLite (multiple servers require etcd, and SQLite→etcd is a one-way migration).
**Node layout → 1 dedicated tainted CP + 3 workers.**
- Isolates the control plane from workload spikes, allows clean independent reboots/upgrades, and spreads workloads better than 2 workers. The CP can be small (2 vCPU / 4 GB).
- *3 nodes (tainted server + 2 agents)* considered: fewer VMs but less spread and isolation.
**Node OS → Flatcar (k3s via sysext); Ignition via config drive.**
- *PXE / Ignition HTTP server* rejected: bare-metal/fleet pattern, unneeded for VMs.
- *Binary in `/opt/bin`* rejected for the k3s install: use the **K3s sysext** instead (immutable, systemd-sysupdate, Renovate-trackable).
- *Ignition delivery* — **decided: cloud-init config drive (`cicustom` user-data), not fw_cfg/`args`** (full reasoning in the dedicated entry below). Validate on a single hand-built node before generalizing into the Ansible loop (§3.1). *(Supersedes both the earlier "cloud-init drive is the clean path" call and the later "community modules lean on fw_cfg" framing — we land back on the config drive, now for an explicit root@pam reason.)*
- Auto-updates coordinated with FLUO so reboots cordon/drain first.
**Persistent storage → ceph-csi (ceph-csi-operator) against the existing Proxmox Ceph.**
- *In-cluster Rook-managed Ceph* rejected: don't run a second Ceph when Proxmox already operates one.
- *Rook-external* rejected: now sits on ceph-csi-operator anyway; heavier than ceph-csi-direct when Proxmox owns Ceph's lifecycle.
- *local-path* rejected: node-bound PVs, failover waits for VM restart. *Longhorn/OpenEBS* rejected: worse on virtual disks and need the NVMe-TCP module Flatcar lacks.
- RBD (RWO) chosen so a dead worker reschedules the pod and re-attaches the volume on a healthy node.
**Provisioning / IaC → Ansible (REVERSED — was Terraform/OpenTofu + bpg).**
- *Original call:* tools split by layer — Terraform/bpg owns VM lifecycle, Ignition the node config, Flux the cluster, Ansible the Mac — with "Terraform's state/plan/drift: don't trade that away." On reflection, for a single-operator lab of 1 CP + 3 *static* workers, that bought drift detection and incremental reconciliation we don't need, at the cost of a fourth tool and a Terraform state file.
- *Why Ansible now:* Ansible is **already** in the stack for the Mac, so folding VM provisioning into it removes Terraform entirely (4 tools → 3: Ansible, Ignition/Butane, Flux). Ansible is **stateless** — it queries Proxmox for live state rather than persisting a state file — which kills Terraform's state-secret problem: no plaintext-secret state file to store securely, lock, or lose, and the Bitwarden "secret zero" (§3.6) is seeded transiently from Ansible Vault instead of landing in TF state. The state file was a specifically-disliked part of Terraform here.
- *What we give up (accepted):* `terraform plan` previews, drift detection, and rigorous state-diff reconciliation. The goal is **consistent rebuild**, not drift management — and that survives intact: a version-controlled playbook *is* a codified rebuild (Ansible recreates VMs + Ignition → Flux repopulates from Git → Ceph + backups hold data). Idempotency becomes module-best-effort (proxmox existence checks) rather than a state diff — irrelevant for clean-slate rebuilds.
- *Ansible's scope now:* Flatcar template build (also collapses in what would've been a standalone shell script), per-node Butane via Jinja2, `butane --strict` transpile, snippet upload, VM clone/delete via the proxmox module, `cicustom` attach, Flux bootstrap — plus the Mac (§3.4, §8).
- *Residual wrinkles:* `proxmox_kvm` may not expose `cicustom` directly — fall back to an API call or a delegated `qm set --cicustom` task (no root@pam needed for that, unlike fw_cfg `args`); and Ansible-driving-Flatcar is less documented than Terraform+bpg, so more of the role is hand-built.
- *bpg vs Telmate, kept for history:* when Terraform was still in scope, `bpg/proxmox` was chosen over the unmaintained Telmate provider. Moot now, but recorded in case Terraform is ever reconsidered.
**Ignition delivery → cloud-init config drive (`cicustom` user-data), not fw_cfg/`args`.**
- *Decision:* each node's Ignition is set as the config drive's **user-data** via `--cicustom "user=<storage>:snippets/<node>.ign"`; the proxmoxve image's default OEM reads Ignition from there. The Proxmox cloud-init GUI fields go inert (Flatcar can't consume Ignition *and* cloud-config in the one user-data slot), so identity/keys/networking all live in the Ignition; DHCP sidesteps the fragile config-drive network-data path.
- *fw_cfg `file=` rejected:* it requires setting QEMU `-args`, which Proxmox **locks to root@pam** — at odds with scoped-token automation. The config-drive route works without root (snippet upload needs host file access, not root@pam). The oft-cited "comma-escaping pain" is specific to the inline `string=` fw_cfg variant; `file=` avoids it, but the root@pam requirement remains. Community bpg+Flatcar modules historically used fw_cfg mainly because they **predate the proxmoxve image** (the generic qemu OEM reads fw_cfg).
- *Still spike-validated:* confirm the proxmoxve OEM consumes raw Ignition JSON cleanly from the config drive on one hand-built node before generalizing.
**GitOps → FluxCD.**
- *ArgoCD* rejected: heavier, and its Application/AppProject + ConfigMap config is fiddly to manage purely declaratively (prior experience). Flux's CRD-native model fits "manifests all the way down."
**Datastore secrets-at-rest → k3s `--secrets-encryption` (aescbc) + full-disk encryption.**
- *KMS-as-KEK* deferred: stronger key separation + audit, but adds a cold-start dependency on KMS reachability at reboot.
**GitOps app secrets → ESO + Bitwarden Secrets Manager.**
- *SOPS + static age key* rejected: one key decrypts everything, and ciphertext in Git history means a future key leak retroactively exposes all of it.
- *SOPS + cloud KMS in Git* rejected: a revocable key fixes the blast radius, but ciphertext still accumulates in Git and it's more parts.
- *SOPS + Flux Bucket/OCI source* rejected: meets zero-trust + not-in-Git, but needs an encrypt-push pipeline and a key to manage.
- *SOPS ciphertext stored in a cloud secret manager* rejected: doesn't work natively — ESO copies values verbatim and never decrypts SOPS.
- *Cloud secret manager directly (GCP SM / AWS SSM) via ESO* rejected: the provider sees plaintext, and it adds vendor sprawl.
- *Self-hosted Vault/OpenBao* rejected: operational weight (seal/unseal, HA) and a security service to keep patched.
- *Self-hosted Infisical in-cluster* rejected: circular bootstrap dependency.
- *Vault Agent injector* rejected: needs Vault live at pod-start; ESO instead materializes persistent K8s Secrets, so workloads restart offline.
- Bitwarden SM chosen: zero-knowledge (provider can't read secrets), already in use (no new vendor), free tier covers it, scoped + expiring machine token, and ESO keeps the backing store swappable (anti-lock-in).
**Control-node secrets → BWS read at run time; `vault.yml` retired. (DECIDED 2026-08-17.)**
- *Supersedes two earlier statements that contradicted each other* — §3.6/§5's "secrets live in **Ansible Vault**, the BW token is seeded from Ansible Vault" (Ansible Vault as the root of trust) and the root `CLAUDE.md`'s later "BWS → `vault.yml` (a materialized cache)" (BWS as the root, vault.yml derived). The arrow reversed between them and nothing recorded it, which is precisely how the question got asked again months later. **BWS is the store; there is no `vault.yml`.**
- *Ansible Vault as the durable store* rejected: no rotation, revocation, or audit; one passphrase gates every secret at once; and it is a second place credentials live that must be hand-reconciled with BWS forever. These are the same properties that ruled out committed ciphertext — a local encrypted blob just moves them off Git rather than fixing them.
- *`vault.yml` as a BWS-materialized cache* rejected (this was the recorded position, now overturned): it leaves **two** secrets to manage (the vault passphrase *and* the BWS token) and **two** sources that diverge silently — a stale cache is byte-indistinguishable from a fresh one until something breaks. It also preserves the long-lived encrypted blob of every credential in the working tree, which is the artifact BWS was adopted to remove.
- **Runtime BWS lookup chosen:** one source of truth, rotation takes effect on the next play run with no re-materialize step, every read is audited per-secret, and the machine token is scoped read-only to a single project with an expiry.
- **Secret zero is irreducible — it relocates, it does not disappear.** The BWS credential can never itself come from BWS. It lives in the **macOS Keychain** and is read at task time (`security find-generic-password -w -s BWS_ACCESS_TOKEN -a <account>`, the retrieval Bitwarden documents). Nothing secret then remains in the repo directory at all. A Keychain item with no trusted application prompts on each access, so the interaction becomes **Touch ID instead of a typed passphrase** — the same shape as `--ask-vault-pass`, with a scoped expiring token in place of a long-lived passphrase.
- *"But provisioning must work offline"* — considered and dismissed as a **non-scenario**. Provisioning is already internet-dependent at every step: the Flatcar image download, the k3s sysext from `extensions.flatcar.org` on first boot, the tigera-operator chart from `docs.tigera.io`, and every container image. BWS adds no new *class* of dependency. If the network is down you are fixing the network, not building a cluster.
- *Residual risk:* a Bitwarden-specific outage while the internet is otherwise up. Covered by the periodic encrypted SM export already in the break-glass plan — that export is the offline copy, not a second live path.
- **BWS layout → one secret per value, NOT grouped.** ~22 secrets in a single `homelab-infra` project, names mirroring today's `vault_*` keys. The free tier caps *projects* (3), not secrets, so the count is free. This preserves per-secret rotation and per-secret audit, and keeps editing to pasting into a field.
  - *Grouping several values into one secret as JSON* rejected. **A BWS secret has no fields** — it is Name + Value + Notes, where Value is a single opaque string, with nothing like Password Manager's custom fields. "Grouping" therefore means hand-authoring JSON into a plain textarea with no syntax awareness and no validation, where a missing comma surfaces as an Ansible failure much later. It also coarsens rotation (changing one credential rewrites the blob) and audit (the log names the group, not the field). Considered specifically to cut API calls — see the next bullet for why that turned out to be the wrong lever.
- **Fetch → a small custom Ansible module wrapping `bitwarden-sdk`, not the stock lookup.** One or two API calls per run regardless of secret count, returning a name→value dict. Wired in as `tasks/load-bws-secrets.yml` at the top of each play — the pattern `load-node-map.yml` already establishes — so `vars.yml` keeps its shape and just sources `{{ bws.<name> }}` instead of `{{ vault_<name> }}`.
  - *The stock `bitwarden.secrets.lookup` used per-variable* rejected: it takes **one secret UUID per call** and has no name lookup and no list operation (`INVALID_SECRET_ID_ERROR: "The secret ID must be a UUID"`; the collection ships exactly one plugin). That means ~22 API calls **and** ~22 UUIDs replacing readable names in `vars.yml` — worse than what it replaces on both counts.
  - ⚠ **The call count is not academic.** BWS enforces **undocumented** rate limits with no published threshold on any tier. Reports on Bitwarden's own forum describe throttling after **six calls in succession**, and one specifically from "an Ansible playbook which looks up a dozen or so secrets in quick succession" — this exact workload. Design for one bulk fetch, not for a number.
  - *Shelling out to `bws secret list`* rejected: it would give one call and readable names, but shelling out is brittle (output parsing, binary on PATH, version drift) when the SDK is already a dependency.
  - **The limitation is the plugin's, not BWS's** — this is the bit worth remembering. The underlying SDK already exposes `list()`, `sync()` and `get_by_ids()`; the collection simply doesn't surface them. So the fix belongs in ~50 lines of our own code over Bitwarden's own SDK, not in contorting the secret layout to fit a plugin's addressing model.
  - **⚠ Do NOT persist a state file — opt out explicitly.** The stock plugin defaults to `state_file_dir: ~/.config/bitwarden-sm-ansible`, and `bws` to `~/.config/bws/state` (one file per access-token id). Bitwarden describes these as *"fully encrypted files that store authentication tokens and additional relevant data"* whose purpose is to *"reduce rate limiting **while authenticating**"*.
    - **Neither the validity period nor the encrypting key is documented**, and the implementation sits in an upstream crate outside `sdk-sm`. The token format (`<version>.<client_id>.<client_secret>:<encryption_key>`) plus per-token file naming *suggests* it's sealed with material derived from the access token — which would make the file useless alone — but that is **inference, not verified**. "Additional relevant data" may include the organization key, which is not short-lived.
    - It is unnecessary here regardless: state files pay off across **many authentications**, and one bulk fetch per play run authenticates **once**. So the benefit is ~nil while the cost is a persisted, unspecified encrypted artifact on disk.
    - That cost lands squarely on the point of this whole decision — getting long-lived secret material out of the working tree. Swapping `vault.yml` for an undocumented blob in `~/.config` is not the trade being made. `bws` opts out via `state_opt_out` in `~/.config/bws/config`; the custom module simply never writes one.
- *Dependency:* the **`bitwarden-sdk` Python package** in `pyproject.toml`, pinned deliberately rather than floating (open SDK-compatibility issue upstream, `bitwarden/sm-ansible#59`). If the stock collection is pulled in as well, note it is single-purpose — the same call already made for `community.proxmox` over the ~800-module `community.general`, which this repo deliberately dropped.
**CNI → Calico.**
- *Cilium* rejected: prior hard struggle to set up and configure; Calico delivers CNI + NetworkPolicy without the eBPF-stack friction. (Revisit eBPF only if you later drop kube-proxy.)
**Load balancer → MetalLB in BGP mode (not L2).**
- *No load balancer at all* was the original gap — Gateway API / NGF needs LoadBalancer IPs and nothing in the design provided them.
- *L2 mode* rejected: BGP gives a dedicated **routed** range (cleaner than carving node-VLAN host space), ECMP spread + faster failover across workers, and it pairs with `externalTrafficPolicy: Local` to preserve client IPs while still distributing. Peer all workers (no SPOF). FRR gotcha: must enable "Disable eBGP Require Policy" or it refuses the routes.
- **OPEN — NEEDS MORE DISCUSSION (raised 2026-07-12): could Calico BGP absorb MetalLB?** Since we're already standing up a BGP session to FRR for the LB range *and* Calico is already the CNI, Calico's own BGP could advertise LoadBalancer IPs directly — **dropping MetalLB** (one fewer tool + one fewer BGP speaker for FRR to manage), and if we also move the *dataplane* to BGP it **removes VXLAN encapsulation** (less per-packet CPU, and no ~50-byte encap tax shrinking pod-path MTU below 1500). Costs: it couples pod networking to the pfSense BGP fabric (bigger blast radius on the base CNI layer) and leans on Calico's **newer LoadBalancer IPAM** (less mature than MetalLB's allocation). This is worth investigating precisely because we're paying for BGP setup anyway. **Decision for now — crawl before walk:** ship the simple, decoupled combo (Calico **VXLAN + bgp:Disabled**, MetalLB owns LB) first, then revisit consolidating onto Calico BGP once the upstream question — *do we want the pod dataplane on BGP/no-encap at all?* — is settled. Tracked in `ansible/CLAUDE.md` §7.
**Cluster CIDRs → `10.0.0.0/8` (never CGNAT).**
- *CGNAT `100.64.0.0/10`* rejected despite the usual "avoids RFC1918 conflicts" appeal — it's a trap in this stack: Tailscale installs a route for the whole `/10` and drops non-`tailscale0` traffic, and Cloudflare reserves chunks of it. The LAN's RFC1918 range is already taken by LAN/Proxmox/Ceph, so cluster CIDRs live in the free `10/8` (`10.42/16` pods, `10.43/16` services).
**Local DNS → split-horizon (not all-through-Cloudflare).**
- *Routing local traffic through Cloudflare Tunnel* rejected: it makes LAN access WAN-dependent (breaks local-first) and causes NAT reflection/hairpinning — the prior "NAT oddities." Split DNS returns the Gateway's MetalLB IP internally (LAN stays local, no NAT), Cloudflare serves the public view, and Tailscale split DNS points the tailnet at the internal resolver; DNS-01 wildcard certs keep TLS valid on every path. (Only *local* access survives a WAN outage — remote access during your own WAN outage is unsolvable.)
