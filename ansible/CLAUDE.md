# CLAUDE.md — ansible/ (Flatcar VM / k3s bring-up)

Nested memory for work inside `ansible/`. Loads automatically when Claude
reads a file in this subtree; doesn't pollute sessions working elsewhere in
the repo (e.g. `gitops/`). Project-wide facts (architecture, Ceph network
topology, standing guardrails) live in the root `CLAUDE.md` — this file is
the detailed, currently-in-progress task state that sits on top of those.

Full design rationale lives in `../docs/mac-studio-inference-stack-2.md`.
When in doubt about *why* a choice was made, check that doc's Appendix A
decision log before re-litigating it. The §6 k3s/multi-node plan is now the
**current** task — §1's VM shell is done.

> **Current task:** **Flux bootstrap** (Flux Operator + `FluxInstance` +
> secret-zero) — the second half of §6 step 4. Everything before it is done:
> **§1 (VM shell), §2 (k3s all-in-one server), and the Calico prime (§6 step 4,
> first half) are all COMPLETE and verified live** on `snoop-a2o` (§1 done
> 2026-07-07: full §1.4 DoD incl. unattended reboot + from-scratch rebuild; §2:
> k3s server up, API serving, secrets-encryption on, datastore on vdb; Calico
> primed 2026-07-12, re-verified 2026-08-01 — node **Ready**, all
> calico-system/calico-apiserver/tigera-operator pods Running, `tigera-operator`
> helm release still at **revision 1** and the live `Installation` CR matching
> `values.yaml` exactly, which is the clean state Flux needs to adopt without a
> diff war). §§1–2 below are kept as the reference for the plumbing the rest
> builds on.
>
> **Before writing any Flux/BGP config, settle the decisions in §7 items 13
> (Calico BGP vs MetalLB) and 14 (scoped kubeconfig for Flux)** — and note the
> secrets-ordering trap in §6 step 5: MetalLB lands *four steps before* ESO
> exists, so its BGP peer password cannot come from an `ExternalSecret`.

---

## 1. DONE — create the Flatcar VM shell ✅

Just the VM. No k3s, no sysext, no cluster config yet. Definition of done is
purely: powered-on Flatcar VM, correctly networked, has a persistent data
disk separate from the OS disk, and is SSH-reachable with key auth — and all
of that survives an unattended `sudo reboot` and a from-scratch rebuild.
**All of this was verified on `snoop-a2o` 2026-07-07** — see §1.4 for the
checklist that passed.

### 1.1 Networking — two NICs, not one

Full topology and rationale live in the root `CLAUDE.md`. Real subnets, VLAN
tags, and bridge names are vaulted (`inventory/group_vars/all/`) — this table
references them by variable rather than literal value:

| Interface | Bridge | VLAN tag | Subnet | Purpose |
|---|---|---|---|---|
| `eth0` (net0) | `{{ dmz_network.bridge }}` | `{{ dmz_network.vlan }}` | `{{ dmz_network.subnet_base }}.0/24` — DMZ | Primary/cluster-facing: SSH, eventual k3s API/pod/service traffic |
| `eth1` (net1) | `{{ ceph_public_network.bridge }}` | `{{ ceph_public_network.vlan }}` | `{{ ceph_public_network.subnet_base }}.0/24` — Ceph public network | Ceph client traffic (ceph-csi → mons on the Ceph public network + OSD client I/O) |

`eth1` must be **tagged** (`{{ ceph_public_network.vlan }}`), not left
untagged on its bridge — untagged there lands on the Ceph
*cluster*/replication-only network, which the k3s nodes have no reason to ever
reach. Isolation on `eth1` comes from the **VLAN itself**, not per-host/port
firewall rules — deliberately, so it scales cleanly as the Ceph cluster grows.

**Jumbo frames must be set end-to-end, not just on the physical side.**
The Ceph-public bridge/bond runs at `mtu 8996`, but that only helps if the
VM's virtual NIC and guest-OS interface also negotiate it — otherwise silent
fragmentation (or a mismatch that quietly caps you back at 1500) undoes the
whole point of moving to this link:
- Set MTU `8996` explicitly on the VM's `net1` hardware definition in
  Proxmox — don't leave it inherited/default.
- Set `MTUBytes=8996` explicitly in the `systemd-networkd` unit for `eth1`
  in Ignition/Butane — don't assume it'll pick up the bridge's MTU.
- `eth0`/DMZ has no jumbo-frame requirement (plain 1Gb bond, no `mtu`
  set) — leave it at the default 1500.

**Implementation notes — static addressing, no DHCP:**
- **Addressing is static, defined directly in each node's Ignition, sourced
  from an Ansible variable file — not DHCP.** This is a different mechanism
  than the "fragile config-drive network-data path" the project doc warned
  about (that referred to leaning on *cloud-init's* `network-data` delivery
  specifically); defining static addresses in Ignition's own
  `systemd-networkd` units sidesteps that path entirely rather than
  triggering it.
- **Source of truth: `inventory/nodes.yml`** — nodes grouped under the k3s
  **cluster** they belong to (`clusters: {<name>: {nodes: {...}}}`); only
  `node_number` is required per node, everything host-shaped derives from it.
  The cluster key IS the cluster's name: it names the kubeconfig
  cluster/user/context and the per-cluster kubeconfig file
  (`ansible/.kube/<cluster>.config`). This is the same node map that drives VM
  creation, so network identity lives right alongside CPU/RAM/role — one place
  to look, one place to change. `playbooks/tasks/load-node-map.yml` flattens
  `clusters` → a cluster-annotated `nodes` map (VMs are provisioned identically
  regardless of cluster) and asserts **global** hostname/`node_number`
  uniqueness — clusters share the DMZ/Ceph subnets and the vmid space, so the
  uniqueness can't be per-cluster.
- **Pin the MAC addresses at VM-creation time** via the `proxmox_kvm`
  module's `net0`/`net1` `macaddr` option, using the same values from the
  node map. This makes Ignition's `[Match] MACAddress=` stanza fully
  deterministic — it no longer matters whether Proxmox/virtio names the
  interface `eth0`, `eth1`, `enp6s0`, or anything else; the network unit
  finds the right NIC by MAC regardless of naming, ordering, or image
  quirks.
- **Render the Butane network units from the node map with Jinja2**
  (`roles/flatcar_vm/templates/`):

  ```ini
  # 00-eth0.network.j2 — rendered per node
  [Match]
  MACAddress={{ eth0_mac }}

  [Network]
  Address={{ eth0_ip }}/24
  Gateway={{ eth0_gateway }}
  DNS={{ dns_servers | join(' ') }}
  ```

  ```ini
  # 10-eth1.network.j2 — rendered per node
  [Match]
  MACAddress={{ eth1_mac }}

  [Network]
  Address={{ eth1_ip }}/24
  MTUBytes=8996
  ```
- `eth1` gets **no `Gateway=`** — it should only reach hosts on the Ceph
  public network, never route anywhere else. Confirm no default route
  appears on `eth1` (`ip route show dev eth1` should show only the
  connected subnet).
- **DNS servers must be set explicitly** in the `eth0` unit (or via a
  separate `systemd-resolved` drop-in) — with DHCP gone, nothing hands the
  VM a resolver automatically anymore. Pull this from `group_vars/all/vars.yml`
  (a `dns_servers` list) so it's not hand-typed per node.
- **Hostname** should also come from Ignition (a `storage.files` entry for
  `/etc/hostname`, rendered from the node map's `hostname` field) rather
  than relying on DHCP option 12 or reverse DNS, now that DHCP isn't in the
  loop at all.
- All of this stays fully rebuildable: delete the VM, re-run the Ansible
  play, and the same MAC + same static IP + same hostname come back — no
  DHCP server, no lease table, no reservation to keep in sync anywhere
  outside Git.

### 1.2 Local data disk

- Attach a **second virtual disk** (separate from the OS/boot disk) for
  local data — this becomes the model/weights or general working storage
  volume later; keep it decoupled from the OS disk so the VM's boot disk can
  be rebuilt independently of accumulated data.
- Partition and mount it via Ignition's `storage.filesystems` /
  `storage.disks` stanzas (Butane `storage` section), not a manual `mkfs`
  after boot — the goal is a disk that comes back correctly formatted and
  mounted on a from-scratch rebuild without manual steps.
- Mount point is **`/var/lib/rancher`** — deliberately k3s's default data-dir
  root, so k3s runs stock (no `data-dir` override) with its state on vdb. See §2:
  overriding `data-dir` to a custom path broke `k3s secrets-encrypt`/tooling that
  assume the default; mounting the disk *at* the default is the fix.

### 1.3 SSH access

- Inject the SSH public key via Ignition (`passwd.users[].sshAuthorizedKeys`
  in Butane), targeting a dedicated, low-privilege user rather than `core`
  left as the only account (matches the Mac-side pattern in the design doc).
- No password auth — key-only. Confirm this explicitly in the rendered
  Ignition rather than assuming Flatcar's defaults; Flatcar's `core` user
  behavior can differ from expectations if the Butane config doesn't set it
  cleanly.

### 1.4 Definition of done
- VM boots with the **static** addresses from the Ansible node map on both
  `eth0` (DMZ) and `eth1` (Ceph public network) — each on its vaulted
  subnet/VLAN — confirmed via `ip a` over SSH; no DHCP lease involved anywhere.
- Each interface's MAC matches what Ansible pinned in the `proxmox_kvm`
  `net0`/`net1` definition, confirmed via `ip link show`, so the
  `[Match] MACAddress=` in Ignition is provably doing the binding rather
  than luck of interface-naming order.
- `eth1` shows the negotiated MTU as 8996, not 1500 — confirmed via `ip a`
  or `ip link show eth1`, since a silent fallback to 1500 defeats the point
  of being on this link at all.
- `eth1` has **no default route** — confirmed via `ip route show dev eth1`
  showing only the connected Ceph public subnet.
- DNS resolution works over `eth0` (e.g. `resolvectl status` /
  `getent hosts <name>`) using the statically-configured resolvers, not a
  DHCP-supplied one.
- `hostname` matches the node map's `hostname` field, confirmed via
  `hostnamectl`.
- `ssh <user>@<eth0-ip>` works with key auth, no password prompt possible.
- The data disk is present, formatted, and mounted at the chosen path,
  confirmed via `df -h` / `mount`.
- `sudo reboot` with no console attached comes back in the same state.
- Deleting the VM and re-running the same Ansible play reproduces an
  identical result — same MAC, same static IP, same hostname — with no
  DHCP server or reservation involved (this is the real test of
  "rebuildable," not just "worked once").

---

## 2. DONE — install k3s (all-in-one server) ✅

The `flatcar_vm` role now bakes k3s into a node's Ignition when its node-map
`role` is a k3s role (`all-in-one`/`server`/`control-plane` → server;
`agent`/`worker` → agent — **only the server path is built so far**). k3s is
delivered via the Flatcar **k3s sysext** (design §3.1), not a binary drop. A
from-scratch rebuild comes up as a running k3s server with no manual steps —
same immutable-provisioning property §1 proved — though the ~50 MB sysext image
is pulled once on **first boot** (see the initramfs finding below), so k3s is up
~30–60 s after boot rather than instantly. Scope of this step: server up, API
serving, node registered, secrets-encryption on, datastore on the data disk,
auto-update machinery wired. The node stays **NotReady** until Calico (its CNI)
arrives — that's a later milestone (§6), not this one.

**What was added (all in `roles/flatcar_vm/` + `group_vars`):**
- `group_vars/all/vars.yml`: `k3s_version` (seed asset, Renovate marker),
  `k3s_minor` (sysupdate feature/MatchPattern pin), `k3s_cluster_cidr`/
  `k3s_service_cidr` (pinned, guardrail §8), `k3s_token`/`k3s_tls_sans`
  (vaulted). `vault.example.yml` gains `vault_k3s_token`/`vault_k3s_tls_sans`.
- `preflight.yml`: derives `k3s_enabled`/`k3s_role`/`k3s_taint`, asserts the
  role is known and the join token is present when enabled. All-in-one gets
  **no** CP taint (added when workers arrive, §6.1).
- `templates/k3s-config.yaml.j2` (server `/etc/rancher/k3s/config.yaml`) and
  `templates/k3s-sysupdate.conf.j2` (minor-pinned transfer config), rendered to
  `files/` by a `k3s_enabled`-gated task in `main.yml`, pulled into
  `butane.yaml.j2` via `contents.local:` (the same pattern as the networkd
  units). `butane.yaml.j2` gained its first `{% if %}` block + `storage.links`
  and `systemd` sections.

**Findings baked into the implementation (were unknowns — design §7 item 5):**
- **Ignition can't fetch the sysext in this env — the image is downloaded on
  first boot instead.** The first attempt used `storage.files` with
  `contents.source:` (a remote fetch), which **boot-looped**: Ignition's files
  stage runs in the **initramfs**, which has **no network** here — no DHCP (hard
  guardrail) and the static `eth0` config only activates *after* the pivot. So
  the remote fetch hangs pre-pivot, Ignition never completes, and Flatcar
  reboots into it forever (symptom: console dead-ends in the initramfs disk
  stage, no shell, never reaches `Welcome to Flatcar`). Fix: a
  `k3s-sysext-download.service` oneshot (`After=network-online.target`) pulls the
  `.raw` from the **real root** (network up — proven), symlinks it into
  `/etc/extensions/`, re-merges systemd-sysext, then starts k3s. It's
  `Condition`-guarded on the `.raw` path so it runs only on first boot / rebuild;
  later boots merge the cached image early and start k3s via the wants/ symlink.
  **Rule for this repo: never put a remote `contents.source:` in Ignition — the
  initramfs has no network. Fetch post-pivot from a systemd unit instead.** We
  also do **not** let Ignition create `/etc/extensions/k3s.raw` (a dangling
  symlink at the early sysext merge could break the docker/containerd sysexts);
  the download unit owns that symlink.
- **The k3s sysext ships NO auto-enable drop-in** (unlike `kubernetes.sysext`).
  The `k3s.service` unit lives *inside* the sysext, so it's absent at
  Ignition-provision time → we enable it with a **`storage.links` wants/ symlink**
  (`…/multi-user.target.wants/k3s.service`), NOT `systemd.units[].enabled`
  (which would try to enable a not-yet-existing unit and fail).
- **sysupdate feature name embeds the minor** (`k3s-<minor>`): the transfer conf
  lives in `/etc/sysupdate.k3s-<minor>.d/`, `MatchPattern=k3s-<minor>.@v-%a.raw`,
  and the update is driven by `systemd-sysupdate -C k3s-<minor> update`. The name
  must match across all three. A minor bump = change `k3s_minor`/`k3s_version`
  (Kubernetes has no unattended minor upgrades). Mirrors the sysext-bakery.
- **Auto-update needs an explicit trigger**: the base
  `systemd-sysupdate.service` only updates the OS, so a drop-in runs the
  `-C k3s-<minor> update` as `ExecStartPre` and flags `/run/reboot-required` if
  the active `.raw` changed (the new k3s binary applies on next boot, not
  hot-swapped under the running service). **Patch pull-through is now PROVEN
  live** (2026-08-01, closing §7 item 5) — see that item for the evidence.
  Practical consequence: `k3s_version` in `group_vars` is only the **seed**
  asset for a fresh node, not what a long-running node runs. `snoop-a2o` was
  seeded at `v1.32.2+k3s1` and now runs `v1.32.3+k3s1`, so expect a
  provisioned-vs-running delta and don't treat it as drift. Bump the seed pin
  periodically anyway, purely to shrink the first-boot catch-up download.
- **k3s datastore on the data disk — via the default path, NOT a `data-dir`
  override.** vdb is mounted at **`/var/lib/rancher`** (k3s's default data-dir
  root), so the kine/SQLite datastore + embedded containerd + image cache (the
  disk-eaters) live on vdb, off the OS disk, while k3s runs stock. `k3s.service`
  gets an `After=systemd-sysext.service` + `RequiresMountsFor=/var/lib/rancher`
  drop-in so the binary exists and the disk is mounted before it starts. **Why
  not `data-dir: <custom>`:** the first cut did that (`/var/lib/data/rancher/k3s`)
  and `k3s secrets-encrypt` (+ `etcd-snapshot`, the uninstall script, community
  tooling) broke — they assume the default path and need `--data-dir` under an
  override. Mounting the disk *at* the default sidesteps all of it. (kubeconfig
  is unrelated — always at `/etc/rancher/k3s/k3s.yaml`.)

**DoD for this step:** see `ansible/README.md` → "Verify (definition of done)"
(the k3s section). The node must come up identically from a destroy+re-run, same
as the §1 shell rebuild.

**Not in this step (later milestones):** CP taint (§6.1), agent/worker join
config (§6.2), Flux bootstrap + Calico (§6.4–5, see §6 note below), full-disk
encryption (design §3.6), moving kubelet's ephemeral root to vdb, backing up the
aescbc key off-cluster.

---

## 6. k3s / multi-node plan (step 4's Calico half DONE; steps 1–3, 5–7 open)

Once Phase 1 is solid, expand in this order (mirrors the project doc's
bring-up order, but starting from an already-running single node instead of
from zero):

1. **Add the CP taint** to the existing node's config now that workers are
   coming: `node-role.kubernetes.io/control-plane=true:NoSchedule`.
2. **Provision 3 worker VMs** the same way (Flatcar + Ignition + k3s sysext),
   joining with the minimal agent config (server URL + token) — not the full
   flag set above, that's server-only.
3. **Fold provisioning into Ansible**: turn the hand-validated Ignition/k3s
   process into a role, loop over the node map, generalize to 1 CP + 3
   workers. Template build (Flatcar image → import → convert) is its own
   idempotent Ansible role.
4. **Install Calico from Ansible, then hand it to Flux** (see the "Calico
   bootstrap" note below) and **bootstrap Flux** as the final steps of the same
   Ansible run (Flux Operator + `FluxInstance` pointed at the Git repo — see
   `gitops/`).
   - **The Calico-prime half is DONE and verified live** (primed 2026-07-12,
     re-verified 2026-08-01) in
     `playbooks/bootstrap-cluster.yml` (wired into `site.yml` after
     `provision-nodes.yml`): it waits for SSH, polls the k3s API `/readyz`,
     fetches `/etc/rancher/k3s/k3s.yaml` and rewrites it — `server:` → the DMZ IP,
     and the entries renamed off k3s's `default` to the cluster key from the node
     map (k3s names cluster+user+context ALL `default`, so two clusters would
     silently clobber each other on any merge) — landing at
     `ansible/.kube/<cluster>.config` (git-ignored) and *merged* (not overwritten)
     into `~/.kube/config` via `kubernetes.core.kubeconfig` when
     `kubeconfig_merge_user`. The play is **per-cluster**: it elects one bootstrap
     primary per cluster in the node map (in-memory group `k3s_primaries`) and
     primes each cluster's Calico against that cluster's own kubeconfig — only one
     cluster exists today, so that path is structural, not hardware-tested. Then
     `helm`-installs the
     `tigera-operator` chart from `gitops/infrastructure/calico/values.yaml`
     (`calico_version` pin) and waits for the node to go Ready. **Verified
     2026-08-01:** node Ready, all calico pods Running, helm release at
     **revision 1** (primed once, never re-applied) and the live `Installation`
     CR matching `values.yaml` — `bgp: Disabled`, `cidr: 10.42.0.0/16`,
     `VXLANCrossSubnet`, `nodeAddressAutodetectionV4.kubernetes: NodeInternalIP`.
     That revision-1-with-no-diff state is precisely what makes the Flux
     adoption a quiet takeover, so **re-check it if the prime is ever re-run
     before Flux lands**. The **Flux bootstrap half** (Flux Operator +
     `FluxInstance` + secret-zero, which then *adopts* that release) is still
     TODO — the next milestone.
   - **Flatcar gotcha (baked in): the node has NO Python**, so the tasks that run
     *on* it (poll `/readyz`, read the kubeconfig) use **`raw`** (straight over
     SSH, no interpreter), with `sudo` embedded in the command — NOT
     `command`/`slurp`, which need a target Python and would fail with "python
     not found". The Helm/`k8s_info` tasks avoid this entirely by running in a
     `hosts: localhost` play against the cluster via kubeconfig — nothing k8s is
     installed on the node. **Rule for any future Ansible-on-node work** (agent
     joins, Flux-over-SSH): use `raw`, or the play won't run on Flatcar.
   - The pinned Calico definition Flux adopts lives at
     `gitops/infrastructure/calico/` (HelmRelease + HelmRepository +
     `values.yaml`, the single values source Ansible also primes from). See
     `gitops/CLAUDE.md` for the adoption mechanics and the repo's 3-tier layout
     (`deployment/` entrypoints + `infrastructure/` + `apps/`).
5. **From Git, in dependency order** (Calico already primed in step 4): MetalLB
   (BGP) — *but see §7 item 13: whether Calico BGP should absorb MetalLB is an
   open question; we ship MetalLB first and revisit* → NGINX Gateway Fabric +
   cert-manager (DNS-01 wildcard) →
   ceph-csi-operator + StorageClasses → External Secrets Operator + Bitwarden
   SDK Server → Postgres + Redis → LiteLLM → confirm a chat completion
   round-trips to the Mac.
   - **⚠ Secrets-ordering trap — MetalLB needs a secret four steps before ESO
     exists.** MetalLB's BGP session wants a **peer password** (`BGPPeer.spec.
     password`, a `Secret` ref), and the peer IPs/ASNs are topology, which this
     repo vaults by convention. But ESO — the answer to "where do secrets come
     from" — is *fourth* in the very chain above, and it can't be pulled earlier:
     the Bitwarden SDK Server needs a cert-manager cert, cert-manager's issuance
     path wants a Gateway, and the Gateway needs a LoadBalancer IP from MetalLB.
     The cycle is real, so **MetalLB's BGP credential must come from outside the
     ESO path** — the live options being an Ansible-primed `Secret` from the
     vault (consistent with how Calico was primed) or a secret-zero-style
     `Secret` created during Flux bootstrap. **Decide this before writing the
     MetalLB HelmRelease**, not when it fails to reconcile. Related: §7 item 13
     — if Calico BGP absorbs MetalLB, this trap moves onto Calico's `BGPPeer`
     rather than disappearing.

> **Calico bootstrap — Ansible installs once, Flux adopts (decided 2026-07-08).**
> The chicken-and-egg is real: Flux's own pods need a CNI, but Calico (the CNI)
> is meant to be Flux-managed. Resolve it by having **Ansible install Calico once**
> during bootstrap, then letting **Flux adopt** the same release — *not* by baking
> a k3s autoload manifest (`/var/lib/rancher/k3s/server/manifests/`). Why not
> autoload: k3s's AddonManager continuously re-applies autoloaded manifests, so
> Flux would fight it over the same objects, and deleting the manifest to "stop"
> autoload makes AddonManager *prune* (tear Calico down) — clean only if k3s owns
> the CNI forever. A one-shot Ansible install leaves no lingering reconciler, so
> the handoff to Flux is a quiet takeover. Mechanism: keep **one** pinned Calico
> definition in Git (`gitops/`, as the Flux HelmRelease/Kustomization); Ansible
> primes that **same** release name+namespace/manifests once (e.g. `helm install`
> or `kubectl apply --server-side`) so Flux's first reconcile matches desired
> state (no diff war). Fixed order: k3s up → install Calico → wait node Ready →
> Flux Operator + `FluxInstance` + secret-zero. Shared prerequisite (also needed
> for the Flux bootstrap): fetch `/etc/rancher/k3s/k3s.yaml` over SSH and rewrite
> its `server:` URL to the DMZ IP so the Ansible control node has cluster access
> right after k3s comes up. The current §2 k3s step is already forward-compatible
> (it bakes **no** autoload manifest and keeps `flannel-backend: none`).
6. **Then**: Qdrant → RAG/orchestrator → Open WebUI → OTel Collector.
7. **Then**: split DNS + external access (internal resolver, Tailscale split
   DNS, Cloudflare Tunnel), source-IP preservation checks on both paths.

Networking prep (pfSense VLAN, MetalLB `/24`, FRR ASNs, "Disable eBGP
Require Policy") and the Ceph pool/client-user setup can happen in parallel
with Phase 1 — they don't block the single-node milestone.

---

## 7. Unknowns / needs a test harness or PoC before trusting it

Items **0–2 are specific to the current VM-creation task** — resolve these
first. Items 3 onward carry over from the broader design for continuity and
apply once k3s work starts.

0. **Secondary NIC (`eth1`, Ceph public network — the tagged VLAN on its
   bridge) may not come up reliably on Flatcar without an explicit
   `systemd-networkd` unit** — Flatcar's default network config typically only
   handles a single interface out of the box, and VLAN tagging adds another
   way for this to silently go wrong (e.g. the `[Match] MACAddress=` stanza
   failing to bind and the interface coming up unconfigured, or landing on the
   untagged/native cluster VLAN instead of the public one if the VLAN tag
   isn't applied where expected). **Test:** boot the VM, confirm `eth1` comes
   up with the exact static address from the node map on the Ceph public
   subnet (not the cluster/replication subnet, and not unconfigured), with
   *no* default route, and confirm the negotiated MTU is actually **8996**,
   not silently 1500 — a jumbo-frame mismatch between the VM's virtual NIC and
   the bridge is a classic silent failure mode (things work, just slowly,
   with no obvious error).

1. **Data disk stanza in Ignition (`storage.disks`/`storage.filesystems`)
   actually formats and mounts a *second* virtio disk correctly on first
   boot.** **Test:** confirm the filesystem survives a full VM
   delete-and-recreate from the same Ignition (not just a reboot of the
   existing disk) — a stale filesystem signature from a prior attempt can
   cause Ignition to skip formatting if `wipeFilesystem` isn't set
   deliberately.

2. **MAC-address pinning at VM creation must actually stick, or the whole
   static-addressing scheme falls apart.** If the `proxmox_kvm` module (or
   the pinned version of it) doesn't reliably apply the `macaddr` option on
   `net0`/`net1`, Ignition's `[Match] MACAddress=` won't find the interface
   it expects, and the node comes up with no network config on that link at
   all — a much quieter failure than a missed DHCP reservation used to be.
   **Test:** after VM creation, confirm via `qm config <vmid>` (or the
   Proxmox API) that both NICs' MACs match the Ansible node map exactly,
   before relying on Ignition to match against them — do this on the very
   first node, not after the pattern's been copied into a loop.
   Relatedly: **dropping DHCP also drops its free IP-collision protection**
   — nothing stops the same address from being handed to two nodes if the
   node map has a typo or a stale entry. `inventory/nodes.yml` is now the
   **sole** source of truth for address allocation; there's no DHCP lease
   table to cross-check against. Worth a lightweight sanity check (e.g. a
   pre-flight Ansible task or CI lint that asserts uniqueness across all
   `eth0_ip`/`eth1_ip` values in the node map) before it's relied on for
   more than a couple of nodes.

3. **Ignition-via-config-drive actually works on the proxmoxve image.**
   Unverified assumption: the proxmoxve image's default OEM reads Ignition
   cleanly from `cicustom` user-data. **PoC:** hand-build exactly one node
   this way before writing any Ansible role around it. If it fails, the
   fallback (fw_cfg `file=`) needs root@pam, which conflicts with the
   scoped-token automation goal — worth knowing early.

4. **`cicustom` may not be exposed by the pinned `proxmox_kvm` /
   `community.proxmox.proxmox*` module version.** **Test harness:** a small
   Ansible playbook that attempts `cicustom` via the module first; have the
   `uri`/API fallback and the delegated `qm set --cicustom` fallback both
   written and tested, not just theorized.

5. **RESOLVED 2026-08-01 — k3s sysext + Flatcar works end-to-end, including
   unattended patch updates.** (Was: "less documented than a standard binary
   install; confirm it installs, updates via systemd-sysupdate, and starts
   cleanly on first boot.") All three legs are now proven on `snoop-a2o`:
   - *Install + clean first boot:* proven at §2 (after the initramfs-network
     fix — see §2's findings; that failure mode is the lasting lesson here).
   - *Unattended patch update:* the node was **seeded at `v1.32.2+k3s1` and now
     runs `v1.32.3+k3s1`**, entirely on its own. `journalctl -u systemd-sysupdate`
     shows `systemd-sysupdate` selecting update `3+k3s1`, pulling
     `k3s-v1.32.3+k3s1-x86-64.raw` from `extensions.flatcar.org`, and installing
     it — 2026-07-12 18:14, ~3h after the 15:33 seed download. Both `.raw`s are
     retained in `/opt/extensions/k3s/` (under `InstancesMax=3`) and the
     `CurrentSymlink` `/etc/extensions/k3s.raw` was re-pointed to `.3`.
     `k3s --version` confirms the new binary took on the next boot.
   - *Minor pin holds:* it moved within `v1.32` only, never across a minor —
     which is the whole point of the `k3s-<minor>` feature name / `MatchPattern`.
   - **Cosmetic, worth a cleanup:** each run logs
     `Target specification lacks MatchPattern= expression. Assuming same value as
     in source specification.` Harmless (the assumption it makes is the one we
     want), but adding an explicit `MatchPattern=` to `[Target]` in
     `k3s-sysupdate.conf.j2` would silence it and remove the reliance on a
     default.
   - **How to re-verify** after any change here: `ls -la /opt/extensions/k3s/`
     (instances) + `ls -la /etc/extensions/` (where `k3s.raw` points) +
     `sudo journalctl -u systemd-sysupdate | grep k3s`.

6. **Node VLAN subnet — TBD.** Blocks finalizing the MetalLB `/24` and FRR
   BGP peer addresses. Needs to be decided before k3s expansion step 5
   (MetalLB) in §6.

7. **Internal DNS resolver approach — TBD, three options undecided:**
   pfSense Unbound host overrides vs. a single wildcard `*.apps.<domain>` +
   HTTPRoute host routing vs. a second external-dns instance. Pick one and
   test split-DNS behavior (including the Unbound rebinding-protection
   whitelist) before relying on it for the "internal view."

8. **FRR "Disable eBGP Require Policy" gotcha.** On current FRR this must be
   set (or a route-map/prefix-list added) or MetalLB's routes get silently
   refused. **Test:** confirm one MetalLB-assigned IP is actually reachable
   over BGP before assuming the LB layer works — a silent refusal here looks
   like "everything's fine" until you try to reach a Service externally.

9. **ceph-csi version vs. Proxmox Ceph release, and RBD/CephFS image
   features vs. the Flatcar kernel.** **Test:** provision one test PVC (RBD)
   and one CephFS mount from the cluster before trusting either
   StorageClass; watch for feature-flag mismatches (e.g. exclusive-lock,
   object-map) that a specific kernel doesn't support.

10. **Single control-plane node = single point of scheduling failure for API
    server availability**, mitigated only by Proxmox HA restart, not k8s
    quorum. Not really "unknown" — it's an accepted tradeoff — but worth a
    **test**: kill the CP VM's host and time how long the API is actually
    unreachable during the Proxmox HA restart, so you know the real blip
    duration rather than assuming it's negligible.

11. **secrets-encryption re-encryption path.** If `--secrets-encryption` isn't
    enabled at first server start, turning it on later requires a rotation
    procedure. **Test when k3s work starts**, don't defer: verify it's on
    from boot 1, since retrofitting it is its own unverified procedure.

12. **Metal-framework stability under load (Tier 1, not this task, but a
    cross-tier dependency)** — noted in the source doc as flaky on some
    macOS point releases. Not blocking for the VM/k3s work, but if
    end-to-end testing later ("confirm a chat completion routes end-to-end")
    fails intermittently, check this before assuming it's a cluster-side
    bug.

13. **Calico BGP as a MetalLB replacement — OPEN, needs more discussion
    (raised 2026-07-12).** We're currently shipping the simple combo: Calico
    **VXLAN + `bgp: Disabled`** (see `gitops/infrastructure/calico/values.yaml`)
    with **MetalLB** owning LoadBalancer IPs in BGP mode (§6 step 5). But since
    we're already paying to peer BGP with FRR for the LB range, and Calico is
    already the CNI, **Calico's own BGP could advertise LoadBalancer IPs
    directly** — attractive because it would: (a) **drop MetalLB** (one fewer
    tool + one fewer FRR peer to manage); (b) if we also move the dataplane to
    BGP, **remove VXLAN encapsulation** → less per-packet CPU and **no ~50-byte
    encap tax** cutting pod-path MTU below 1500 (an MTU class of bug we
    otherwise invite). **Costs / why not yet:** couples pod networking to the
    pfSense BGP fabric (bigger blast radius on the base CNI layer, the one you
    least want to churn), and leans on Calico's **newer LoadBalancer IPAM**
    (less battle-tested than MetalLB's allocation — verify its maturity at the
    pinned `calico_version`). **Crawl-before-walk plan:** start simple as above,
    revisit consolidation once the real upstream question is decided — *do we
    want the pod dataplane on BGP/no-encap?* If VXLAN stays → keep MetalLB. If
    we go native-routed → then MetalLB is redundant and folding LB advertisement
    into Calico removes a component. **PoC before switching:** stand up Calico
    BGP peering to FRR and confirm a LoadBalancer IP is both *allocated* (Calico
    IPAM) and *reachable* over BGP, next to (not replacing) MetalLB, before
    committing. Related: item 8 (FRR "Disable eBGP Require Policy" applies to
    Calico's session too) and design-doc Appendix A "Load balancer" entry.

14. **Cluster-admin kubeconfig persists on the control node — OPEN, decide WITH
    the Flux milestone (raised 2026-07-12).** `bootstrap-cluster.yml` fetches k3s's
    admin kubeconfig to `ansible/.kube/<cluster>.config` (0600, git-ignored) and
    merges the context into `~/.kube/config`. That admin cert then just *sits*
    there: it's cluster-admin, long-lived, and every play reads it — including the
    Flux bootstrap that's next. **The two questions, which are really one:** should
    Flux get a **scoped ServiceAccount kubeconfig** rather than reusing the admin
    one, and should the admin file be **shredded after bootstrap** once nothing
    needs it? **Why not just delete it in the happy path today** (the obvious move,
    and wrong): (a) later plays run *standalone* — a `flux-bootstrap.yml` on its own
    would find no kubeconfig, working only inside a full `site.yml` that re-fetched
    it first, a coupling that bites exactly once and confusingly; (b) with
    `kubeconfig_merge_user: false` (the CI case) deleting the repo file leaves **no
    kubeconfig anywhere** — a footgun that fires only for whoever opted out of the
    home-dir write. Cheap to defer: the file is regenerated from the node on every
    `bootstrap-cluster.yml` run, so nothing is lost by keeping it until Flux's
    access model is settled. Options if we do act: an opt-in
    `kubeconfig_cleanup_local` flag (guarded on `kubeconfig_merge_user`), or a
    deliberate `playbooks/clean-kubeconfig.yml` teardown (the `kubernetes.core.
    kubeconfig` module's `behavior: remove` can pull the merged contexts back out
    of `~/.kube/config` too) — never as a silent step in the provisioning path.

---

## 8. Guardrails specific to this role

(Repo-wide guardrails — no Terraform, no DHCP, no second Ceph, no CGNAT —
live in the root `CLAUDE.md`. These are additional, specific to
`flatcar_vm`/k3s work.)

- Keep `cluster-cidr` / `service-cidr` pinned explicitly in every node's
  config (not left as k3s implicit defaults) so Calico's IPPool always
  matches, once k3s work starts.
- Server-only k3s flags (the block in §6) do not go on agent/worker join
  configs — workers get only the server URL + token.
- `eth0` (DMZ) and `eth1` (Ceph public network) are the only two networks
  this VM touches. Never leave `eth1` untagged/native on its bridge — that
  lands on Ceph's `cluster_network` (replication-only), which ceph-csi has no
  business reaching.
- `eth1` needs MTU `8996` set explicitly (both in the Proxmox VM hardware
  config and the Ignition network unit) — don't assume it inherits the
  bridge's jumbo-frame setting.
