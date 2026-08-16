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
> **Sequencing changed 2026-08-02: the Calico BGP migration now comes BEFORE
> the Flux bootstrap.** Rationale: BGP is not a late-tier LB feature here — it
> becomes the *dataplane* (§7 item 13 is now RESOLVED: Calico BGP owns both LB
> advertisement and pod routing, no MetalLB). Calico's VXLAN implementation uses
> **no BGP at all**, so going no-encap takes BGP from "not running" to
> "load-bearing for pod networking." Churning the CNI is at its **cheapest right
> now** — one node, no workloads, nothing in `apps/`, no PVCs, and Calico is
> still *Ansible*-managed (helm revision 1), so re-priming is an Ansible re-run
> rather than a fight with Flux. Every week that gets worse.
>
> **Versions APPLIED 2026-08-03: Calico `v3.32.1` + k3s `v1.36.2+k3s1`** (§7
> items 15 + 16). **They deliberately did NOT land with the encapsulation
> change**, contrary to the original "one rebuild instead of two" plan: the BGP
> work is blocked on pfSense values, while the version bump had no external
> dependency, so batching them would have held a ready change hostage AND put
> two variables in one rebuild. `values.yaml` is therefore still
> `VXLANCrossSubnet` + `bgp: Disabled`.
>
> **⚠ The #12890 LoadBalancer-IPAM RBAC workaround is NOT yet applied.** That is
> fine *only* because nothing allocates LoadBalancer IPs yet — it becomes
> load-bearing the moment the BGP work starts, and a gap there stalls the
> Gateway → cert-manager → ESO chain with LB IPs stuck `pending` while BGP looks
> healthy. **Apply it with the BGP change, not after** (item 15). The old
> "MetalLB secrets-ordering trap" is **resolved** — see §6 step 5.
>
> **✅ DONE 2026-08-16 — §7 item 6 is RESOLVED and the pfSense config generator
> is written.** Cluster ASN `64601` (`bgp_asn_base + index`), pfSense AS
> `64512`, LB range `<lb_range_base>.<index>.0/24`, peer IP = `dmz_network.gateway`.
> **Nodes did not move, so nothing was re-provisioned.** Two new vault vars only
> (`vault_lb_range_base`, `vault_frr_master_password`); both ASNs are cleartext.
>
> `playbooks/render-frr-config.yml` renders the pfSense/FRR config and the
> firewall-alias member list from `inventory/nodes.yml` + the vault — verified
> rendering correctly for one and two clusters, and its collision asserts
> verified *firing*, not just passing (the item 18 lesson). pfSense CE has no
> API, so delivery is still a manual paste, but the content is generated and
> asserted.
>
> Two real bugs were found in the runbook while doing this, both silent-failure
> modes: the LB range must **not** live inside the DMZ subnet (item 6), and the
> prefix list needs **`le 32`** or `Local`-policy Services blackhole (item 8).
>
> **Still open before writing the Calico manifests:** whether pfSense/FRR is
> actually configured on the box (§2–§7 of the runbook — the render is done, the
> paste is not), and §7 item 14 (scoped kubeconfig for Flux). BWS unblocks
> nothing (`vault.yml` already works), so it follows rather than leads.
>
> **⚠ Next session, before writing `BGPConfiguration`/`BGPPeer`/`BGPFilter`:**
> confirm the exact CR shape for Calico **3.32** — that release moved the CRDs
> out of the chart (item 15), and the LB range appears in *two* places: the pool
> LB IPs are allocated from (3.32's LoadBalancer IPAM, the feature with the
> broken RBAC grant in #12890) and `BGPConfiguration.spec.serviceLoadBalancerIPs`
> for advertisement. The #12890 workaround must land **with** these, not after.
>
> **✅ DONE 2026-08-03 — versions bumped, and the Calico eBPF migration is
> complete.** The cluster now runs **k3s `v1.36.2+k3s1` + Calico `v3.32.1` with
> `linuxDataplane: BPF` and no kube-proxy at all**, all from committed config and
> verified on a from-scratch rebuild (§7 items 15–17). Source IP is preserved
> under `externalTrafficPolicy: Cluster`, which **collapses the four-row matrix
> in `docs/pfsense-frr-bgp-setup.md` §10 to one row** — re-read that section
> before writing the BGP manifests, it's the main thing tonight changed for them.
>
> Two things the bump dragged in, both resolved and worth knowing before the next
> Calico bump: v3.32 **removed the CRDs from the chart** (new `gitops/crds/` tier,
> server-side applied — item 15), and the PVE **snippet-dir permissions reset**
> on storage activation (now self-repairing — item 18).
>
> **✅ DONE 2026-08-03 — §7 item 21: the Ignition snippet is destroyed after first
> boot.** `provision-nodes.yml` now waits for SSH on every node it provisioned,
> then detaches `cicustom` (API) and deletes the `.ign` (SSH) — so the k3s join
> token no longer lives on shared snippet storage past the one boot that reads it.
> Verified end-to-end on `snoop-a2o` **including a full `qm reboot`**, which is
> the check that matters: the ordering failure mode surfaces at *next start*, not
> at provision time. `ide2` deliberately stays (see the item — it's standard, it
> comes back with every clone, and what PVE generates for it carries nothing
> sensitive).
>
> **That work also uncovered a live bug in item 18's snippet-dir self-repair: it
> only worked when a repair was NEEDED.** A skipped task still registers, so the
> skipped re-stat wiped the good stat and the assert failed against a *healthy*
> directory. Fixed. The general lesson is recorded in item 18 and worth carrying:
> **a fix verified only on the failure it was written for is half-verified.**

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
- `group_vars/all/vars.yml`: `k3s_version_default` (seed asset, Renovate
  marker), `k3s_cluster_cidr`/`k3s_service_cidr` (pinned, guardrail §8),
  `k3s_tokens`/`k3s_tls_sans_by_cluster` (vaulted). `vault.example.yml` gains
  `vault_k3s_tokens`/`vault_k3s_tls_sans_by_cluster`.
- `preflight.yml`: derives `k3s_enabled`/`k3s_role`/`k3s_taint`, resolves the
  cluster-scoped `k3s_token`/`k3s_tls_sans`, asserts the role is known and the
  node's cluster has a token. All-in-one gets **no** CP taint (added when
  workers arrive, §6.1).

  > **✅ CHANGED 2026-08-16 — the join token and TLS SANs are per-CLUSTER, not
  > fleet-wide.** Both are now maps keyed by the cluster name, resolved from
  > `node.cluster`. The token is the credential that admits a node to a cluster,
  > so one shared value made a leak from any cluster a leak for all of them —
  > and k3s derives the datastore bootstrap-data encryption key from it too. TLS
  > SANs are per-cluster by construction: a stable API name or VIP belongs to
  > one cluster, and a shared list puts cluster B's name in cluster A's cert.
  >
  > **Migration is a no-op for a running node**: move the old `vault_k3s_token`
  > value under `vault_k3s_tokens.homelab` and the rendered config is
  > byte-identical, so nothing is re-provisioned. There is deliberately **no
  > fallback** to the old variable — a loud assert beats a silent half-migration,
  > and its fail message spells out the move.
  >
  > ⚠ Those facts are set **unconditionally**, not under `when: k3s_enabled`.
  > `set_fact` persists across the role's per-node loop, so a `when` would leave
  > a non-k3s node holding the *previous* node's token — the same
  > stale-registered-value trap as §7 item 18. Verified on that exact case (a
  > bare VM provisioned after two k3s nodes from different clusters resolves to
  > `''`), not just on the happy path.

  > **✅ CHANGED 2026-08-16 — `k3s_version`/`k3s_minor` are per-cluster too.**
  > `k3s_version_default` in `group_vars` is the fleet default; a cluster
  > overrides it with `k3s_version:` in its `inventory/nodes.yml` block. That's
  > what lets a minor bump be staged on one cluster while another stays put,
  > rather than moving the whole fleet at once.
  >
  > **`k3s_minor` is now DERIVED** from the effective version
  > (`v1.36.2+k3s1` → `v1.36`) instead of stated separately. The two always had
  > to satisfy minor ⊂ version, and keeping that by hand is a two-place edit that
  > drifts. An explicit per-cluster `k3s_minor:` override still wins, and
  > preflight asserts containment on both paths — verified firing on a
  > deliberately mismatched override.
  >
  > **Why this failure mode is worth an assert:** if the seeded sysext falls
  > outside the sysupdate `MatchPattern`, the node boots the *correct* k3s and
  > then silently never updates, because `k3s-<minor>.@v` never matches it.
  > Nothing looks wrong at provision time.
  >
  > `snoop-a2o` resolves to `v1.36.2+k3s1`/`v1.36` exactly as before — no
  > re-provision.
  >
  > ⚠ **Calico was considered for the same treatment and deliberately left
  > fleet-wide** — see the note under `calico_version` in `group_vars/all/vars.yml`.
  > k3s's version reaches a node through Ignition alone, so per-cluster cost
  > nothing; `calico_version` is dual-owned with `gitops/` (the HelmRelease chart
  > version must match for clean adoption, and `gitops/crds/calico/crds.yaml` is
  > per-version), so per-cluster Calico forces a per-cluster gitops layout. Do
  > that when a second cluster exists, not before.
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
5. **From Git, in dependency order** (Calico already primed in step 4):
   **Calico BGP** (replaces MetalLB entirely — §7 item 13, decided 2026-08-02)
   → NGINX Gateway Fabric + cert-manager (DNS-01 wildcard) →
   ceph-csi-operator + StorageClasses → External Secrets Operator + Bitwarden
   SDK Server → Postgres + Redis → LiteLLM → confirm a chat completion
   round-trips to the Mac.

   **Calico BGP — what actually gets applied, and by whom.** Three CRs, none of
   which are Helm chart values, so they do **not** go in
   `gitops/infrastructure/calico/values.yaml` (that file stays the shared Helm
   values source). They live as plain manifests alongside it:
   - `BGPConfiguration` — `serviceLoadBalancerIPs` (advertisement) + ASN.
   - `BGPPeer` — the pfSense/FRR peer. **Bootstrap-tier: Ansible primes it**,
     Flux adopts, same pattern as Calico itself.
   - `BGPFilter` — attached via `BGPPeer.spec.filters`. Exports **only the LB
     range** and explicitly `Reject`s everything else, so **pfSense never learns
     the pod CIDR**. Write the terminal `Reject` explicitly rather than relying
     on default behavior for unmatched routes.

   **All k3s nodes are on the same DMZ subnet (decided 2026-08-02)**, which is
   what makes this simple: Calico's **node-to-node mesh is on by default** and
   auto-peers every node with every other node in the same L2, so **pod-to-pod
   routing needs no `BGPPeer` at all**. The pfSense peer exists only for LB
   advertisement and external reachability. Filters attach to `BGPPeer`
   resources, and the mesh isn't one — so the export filter can't starve the
   dataplane. *Confirm that live once peered* (nodes still learn each other's
   pod CIDRs after the filter lands); the docs don't state it explicitly.

   **Consequence of the filter:** `natOutgoing: Enabled` SNATs pod egress to the
   node IP, so pods reach the LAN while the LAN has **no route back to pods** —
   the asymmetry we want, for free. But this is *route hygiene, not enforcement*:
   nodes still forward for pod IPs, so anything on the node subnet that adds a
   static route reaches pods anyway. Real enforcement is Calico
   `GlobalNetworkPolicy`, which a static route can't bypass. Don't confuse the
   two. Cheap belt-and-braces: an inbound prefix-list on FRR rejecting the pod
   CIDR — you're editing FRR policy anyway because of item 8.

   - **✅ Secrets-ordering trap — RESOLVED 2026-08-02.** (Was: MetalLB's BGP peer
     password is needed four steps before ESO exists, and the dependency cycle is
     real — Bitwarden SDK Server → cert-manager cert → Gateway → LoadBalancer IP.)
     The cycle still exists and **ESO still cannot be moved earlier**; what
     changed is that we stopped trying. **Anything needed before ESO exists is an
     Ansible-seeded `Secret`, sourced from the vault** — consistent with how
     Calico was primed, and it keeps credential ciphertext out of Git entirely.
     That covers the BGP peer password (if we even use one — it's optional, and
     on a DMZ VLAN we control end-to-end it may not be worth it) *and* the
     `cluster-topology` Secret that feeds post-build substitution. See the root
     `CLAUDE.md` "Secrets, credentials, and topology blinding" table for the full
     three-tier split.

   - **Topology in `gitops/` uses `${var}` placeholders, not SOPS.** The BGP CRs
     inherently need the peer IP + ASN, and there's no `NodeInternalIP`-style
     autodetection dodge for a peer address. Solution: commit
     `peerIP: ${bgp_peer_ip}` and let Flux substitute from the Ansible-seeded
     `cluster-topology` Secret via `postBuild.substituteFrom`. Nothing encrypted
     is committed. **Gotcha:** undefined variables substitute to the *empty
     string* and reconcile **successfully** — a missing key gives you a broken-
     but-applied `BGPPeer`. Turn on the kustomize-controller feature gate
     `--feature-gates=StrictPostBuildSubstitutions=true`. See `gitops/CLAUDE.md`.

   - **When Ansible must apply a substituted manifest, use `flux build
     kustomization`, never `kustomize build`.** `postBuild` is a
     *kustomize-controller* feature; plain kustomize doesn't know `${var}` and
     will apply the literal string into the cluster (it's a valid string field,
     so it *succeeds*). `flux build kustomization --strict-substitute` runs the
     same implementation Flux uses. **Its trap:** per the docs, "variable
     substitutions from Secrets and ConfigMaps are skipped in dry-run mode" — so
     `--dry-run` silently drops exactly what you need. Order is: Ansible creates
     the Secret → `flux build kustomization` (with cluster access) → apply. One
     substitution source, two consumers, no reimplementation.

   - **Keep the dual-applied set small.** It is: Calico's Helm values, the BGP
     CRs, and the Flux bootstrap. Everything else should be Flux-only. `flux
     build` is the tool when you need it, not the default posture.

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

The Ceph pool/client-user setup can happen in parallel — it doesn't block the
single-node milestone.

**Networking prep is no longer parallel — it's on the critical path.** Once the
dataplane moves to BGP, the pfSense/FRR side (LB range, the ASNs, the peering
itself, and the eBGP policy config — item 8) gates the Calico migration rather
than sitting alongside it. The **values and the generator are done** (item 6);
what remains is applying it to the box. **Runbook:
`docs/pfsense-frr-bgp-setup.md`** — written to be staged *before* the cluster
side exists. A parked config lists every node in `Active`/`Connect`, retrying;
that's expected.

Note FRR must accept the **pod CIDR** too if nodes ever span subnets; on the
same-subnet design above it only needs the LB range, and the `BGPFilter`
guarantees that's all it's offered.

✅ **DECIDED 2026-08-02 — pfSense FRR is managed as raw config, not via the GUI.**
`maximum-paths` (ECMP) exists only there, isn't exposed in the FRR GUI, and
`vtysh` edits are overwritten on the next Apply. The cost is real and total —
saving a raw config stops the GUI's routing config being applied *at all* — but
we don't hand-maintain the result: it's generated from the node map
(`playbooks/render-frr-config.yml`), which is more reviewable and reproducible
than GUI forms, unversioned by construction. Same pattern as Ignition.

✅ **REVISED 2026-08-16 — explicit `neighbor` statements, not `bgp listen
range`.** Dynamic neighbors were the original *second* reason for raw config.
They're incompatible with giving two clusters distinct ASNs on a shared DMZ
subnet: a listen range maps one prefix to one peer group, and a peer group
carries exactly one `remote-as` **and** one set of prefix lists — so every
cluster would share an ASN and a permitted LB range, each free to advertise the
other's. Separating them would have meant carving the /24 into per-cluster bands
and constraining `node_number` allocation permanently.

Consequences to hold onto:
- **Adding a k3s node costs one pfSense paste** — re-render and paste the whole
  file. Not per *rebuild*, since node IPs derive deterministically from
  `node_number`; only genuinely new nodes. The cluster side still has no
  per-node config (one global `BGPPeer`, no `nodeSelector`).
- **The BGP neighbor list and the §6 firewall alias are the same list**, both
  rendered from `inventory/nodes.yml`, so they can't drift.
- ⚠ **The GUI still starts the daemons.** The BGP tab's Enable is the *"master
  enable switch for BGP routing"* — leave it off and `bgpd` never runs, so the
  raw config is never read and it fails silently. Enable-in-GUI,
  configure-in-raw.
- ⚠ **Raw config can be silently ignored across upgrades**
  ([#7859](https://redmine.pfsense.org/issues/7859) was exactly that). Re-verify
  `show running-config` after every FRR package/pfSense upgrade.

Full config, rationale and verification: `docs/pfsense-frr-bgp-setup.md` §4–§7.

**Migration ordering — the risk window is node 2, not today.** With one node the
mesh has no peers to form, so flipping to no-encap is trivially safe and nothing
can break. The first moment mesh routing carries real traffic is when node 2
joins. So: flip encapsulation now, establish and verify pfSense peering with
only one node at stake, and have **both proven before node 2 exists** — far
better than debugging a peering fault and a join at the same time. Also note
**changing an existing IPPool's encapsulation is not a clean in-place edit**
under the Tigera operator; on an empty single-node cluster the honest path is to
re-prime (or rebuild the node — §1 proved from-scratch rebuild works).

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

6. **LB `/24` + FRR peer address — ✅ RESOLVED 2026-08-16.** All four values are
   decided, and most are now *derived* rather than chosen. **Nodes did not
   move** — they stay on the vaulted DMZ network where `snoop-a2o` has run since
   2026-07-07, so none of this required a re-provision.

   Each cluster declares one `index:` in `inventory/nodes.yml`; its ASN and LB
   range both fall out of it, the same way every host-shaped fact falls out of
   `node_number`:

   | Value | Derivation | `homelab` (index 1) |
   |---|---|---|
   | Cluster ASN | `bgp_asn_base + index` | `64601` |
   | LB range | `<lb_range_base>.<index>.0/24` | index 1 of that supernet |
   | pfSense ASN | fixed, one per *router* | `64512` |
   | pfSense peer IP | `dmz_network.gateway` | already vaulted |

   Only **two** new vault vars exist: `vault_lb_range_base` and
   `vault_frr_master_password`. The peer IP is the DMZ gateway (no CARP on that
   interface — confirmed 2026-08-16; if that ever changes it needs its own var),
   and both ASNs are cleartext constants in `group_vars/all/vars.yml` since a
   private-range AS number reveals nothing.

   ⚠ **The LB range is routed-only and must be attached to NO interface
   anywhere.** An earlier revision of the runbook called for "a `/24` inside the
   DMZ subnet" — that is wrong twice over: pfSense's *connected* DMZ route beats
   an equal-length BGP route on admin distance, and on-subnet clients ARP for an
   address nothing answers (Calico does no L2 for LB IPs — that's MetalLB L2
   mode, which we don't run). `playbooks/render-frr-config.yml` asserts against
   overlap with the DMZ/Ceph/pod/service ranges, but it cannot see the pfSense
   interface list, so that half stays a manual check.

   **The pfSense config is generated, not hand-written:**
   `ansible-playbook playbooks/render-frr-config.yml --ask-vault-pass` renders
   `ansible/.frr/frr.conf` (paste target) and `bgp-nodes.txt` (firewall alias
   members) from the node map. Both git-ignored — `frr.conf` embeds the FRR
   master password. Full runbook: `docs/pfsense-frr-bgp-setup.md`.

7. **Internal DNS resolver approach — TBD, three options undecided:**
   pfSense Unbound host overrides vs. a single wildcard `*.apps.<domain>` +
   HTTPRoute host routing vs. a second external-dns instance. Pick one and
   test split-DNS behavior (including the Unbound rebinding-protection
   whitelist) before relying on it for the "internal view."

8. **FRR eBGP policy requirement (RFC 8212) — ✅ DECIDED 2026-08-02: configure
   real policy, do NOT disable the requirement.** (Originally written for
   MetalLB; now applies to *Calico's* session.)

   FRR 7.4+ implements RFC 8212: an eBGP session with **no** inbound/outbound
   policy discards all routes **in both directions**. The session still reports
   `Established` and no prefixes move — the most common silent failure in this
   setup. Two ways to satisfy it:

   - ~~**Option A** — `no bgp ebgp-requires-policy` (the GUI's "Disable eBGP
     Require Policy").~~ What most MetalLB-era guides say. **Rejected as the
     standing config.** Use only as a temporary bisect step when isolating a
     bring-up fault, then revert.
   - **Option B — apply actual prefix lists (CHOSEN).** Omit the disable
     entirely; the requirement is satisfied *because policy exists*.
     `<CLUSTER>-IN` permits only that cluster's LB range (with `le 32` — see
     below); `<CLUSTER>-OUT` denies everything. The filters are per-cluster
     because they hang off the peer group, which is what separates clusters
     (item 6).

   **Why B, given A is one checkbox:** we want the inbound prefix-list
   regardless, as defense-in-depth. The Calico-side `BGPFilter` is the primary
   control keeping the pod CIDR off pfSense — but it's enforced by the very
   device we're guarding against misconfiguring. A pfSense-side prefix list is an
   *independent* check. Once it exists, disabling the requirement buys nothing
   and throws away a safety net. Both directions must be populated: an inbound
   filter alone still gets outbound routes discarded.

   **Raised stakes since item 13:** with the dataplane on BGP too, a silent
   refusal is no longer only an LB-reachability bug.

   **⚠ A second silent-blackhole mode was found 2026-08-16, with the same
   symptom.** FRR prefix lists match the prefix length *exactly* unless given
   `le`/`ge`, and Calico's advertisement granularity is not fixed: it advertises
   the **whole block** under `externalTrafficPolicy: Cluster` but a **/32 per
   Service** under `Local`. So a bare `permit <lb_range>` accepts the first and
   silently drops the second. The rendered config uses `permit <lb_range> le 32`,
   which covers both — see `docs/pfsense-frr-bgp-setup.md` §4. Two different
   causes now produce "Established session, nothing flowing," which is why the
   test below is reachability, not session state.

   **Test:** confirm one Calico-assigned LoadBalancer IP is actually reachable
   over BGP before assuming the LB layer works — a silent refusal looks like
   "everything's fine" until you try to reach a Service externally. Then assert
   the pod CIDR is absent from `vtysh -c 'show ip bgp'`; if `10.42.0.0/16`
   appears, *both* the `BGPFilter` and the prefix list failed.

   **The `frr.conf` implementing this, plus verification commands:
   `docs/pfsense-frr-bgp-setup.md`** (§4 and §7).

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

13. **RESOLVED 2026-08-02 — Calico BGP replaces MetalLB, for both LB
    advertisement AND the pod dataplane.** The upstream question this item was
    waiting on ("do we want the pod dataplane on BGP/no-encap?") was answered
    **yes**. Consequences, all now tracked above: `values.yaml` moves to
    `bgp: Enabled` + no encapsulation; MetalLB never gets written (§6 step 5);
    BGP config becomes bootstrap-tier because the dataplane depends on it.
    **Key fact that drove the sequencing change:** Calico's VXLAN implementation
    uses **no BGP at all**, so this isn't "enable a feature alongside the
    existing dataplane" — it takes BGP from *not running* to *sole mechanism for
    pod routing*. Two mitigations found: all nodes on one subnet means the
    default **node-to-node mesh** carries pod routes with no `BGPPeer` involved,
    and the risk window is **node 2's join**, not today.
    **⚠ This decision is blocked on a version bump — see item 15**, which is the
    one part that did *not* resolve. Original analysis kept below for the
    reasoning; the "crawl-before-walk / ship MetalLB first" plan in it is
    **superseded**.

    <details><summary>Original open question (2026-07-12)</summary>

    We're currently shipping the simple combo: Calico
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

    </details>

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

15. **`calico_version` → `v3.32.1`, with a mandatory RBAC workaround (raised and
    DECIDED 2026-08-02).** Was a blocker on item 13's decision; now resolved —
    see the DECIDED block below for the pin, the workaround manifest, and its
    removal criteria. Background, verified against upstream docs:
    - **Our pinned `v3.29.1` cannot allocate LoadBalancer IPs — only advertise
      them.** The 3.29 docs say it verbatim: *"Service LoadBalancer address
      allocation is outside the current scope of Calico, but can be implemented
      with an external controller."* Their recommended 3.29 setup is to keep
      **MetalLB's controller** (allocation) with the **speaker disabled**, and let
      Calico advertise. So "drop MetalLB entirely" is simply not reachable at the
      current pin.
    - **LoadBalancer IPAM landed in OSS v3.30.** Needs an explicit `IPPool` with
      `allowedUses: [LoadBalancer]` + `assignmentMode: Automatic`; the LB
      controller is enabled by default in kube-controllers. **Watch the default
      mode:** `AllServices` grabs *every* LoadBalancer Service in the cluster —
      `RequestedServicesOnly` + `loadBalancerClass: calico` is the deliberate
      posture.
    - **Open, unresolved bug — bound to Calico 3.32, NOT to any k8s version.**
      [projectcalico/calico#12890](https://github.com/projectcalico/calico/issues/12890)
      (2026-06-02, still open). LB IPs stuck `pending` while BGP advertises the
      routes fine, because `calico-kube-controllers` can't read `ipamconfigs`
      (the ClusterRole grants `ipamconfigurations`). **Full thread read
      2026-08-02 — the evidence is stronger than a single lab report:**
      - **Three independent parties, two distros.** Reporter on k3s 1.36.1 +
        Debian/Raspbian **arm64** + Mikrotik ROS (four real nodes — *not* kind,
        despite how it may skim); a second reproduction on **rke2 v1.35.5**; a
        third confirming the fix. So it's neither kind-specific, arch-specific,
        nor k3s-specific.
      - **The reporter bisected it.** Two days after filing: *"I have tried the
        same exercise on k3s 1.35.5+k3s1 and calico 3.31. In this configuration
        the LoadBalancer IP's are assigned successfully and routed correctly
        outside of the cluster."* Same person, same hardware, same procedure —
        **3.32 broken, 3.31 works.** Root cause later pinned to PR #11839
        (milestone v3.32.0), which is simply not in 3.31.
      - **Confirmed workaround exists** (extra ClusterRole granting `ipamconfigs`
        to the `calico-kube-controllers` SA in `calico-system`), posted by the
        second reporter and confirmed by the third. **This is what makes the
        3.32.1 pin viable** — we pre-apply it rather than waiting to be bitten.
        See the DECIDED block below for the manifest.
      - **What's genuinely weak:** the reporter self-describes as unsure of the
        root cause, and their own paste has an internal inconsistency (a
        `jsonpath` showing SA `calico-kube-controller`, singular, vs the plural
        in the error). Their repro also pre-installs the v1 CRD bundle by hand
        (`kubectl create -f .../v1_crd_projectcalico_org.yaml`) and uses raw
        manifests rather than the Helm chart we use — so our install path is not
        identical. But the working RBAC fix makes the diagnosis solid regardless.
      - **Zero maintainer engagement after 2 months.** All three commenters are
        unaffiliated, no labels, no linked PR. Treat as unfixed and unattended —
        that part of the original read stands.

    **The two halves are separable, and the risk is lopsided** — this is the
    useful framing for the decision:

    | | Maturity | What it buys |
    |---|---|---|
    | Dataplane → BGP, no encap | Core Calico, mature for years | Kills the ~50-byte VXLAN tax + per-packet CPU. The real win. |
    | LB IPAM → drop MetalLB | New in 3.30, open bug above | Removes one component. |

    The dataplane half is the big, hard-to-reverse change and it's the **low**-risk
    one; the MetalLB removal is cosmetic and carries the risk. **Recommendation:
    bump to ≥3.30 and do the dataplane switch now** (it's free today and gets
    expensive after node 2), and treat LB allocation as a separate call — either
    Calico IPAM with MetalLB-controller-only as a known-good fallback, or take
    the bet knowingly. Either path ends with no MetalLB *speaker* and a single
    BGP session to FRR, which is most of the goal.

    **⚠ SECOND, UNRELATED 3.32 GOTCHA — hit for real 2026-08-02 on the first
    bootstrap at the new pin.** v3.32 **removed the CRDs from the tigera-operator
    chart** (its `crds/` dir is empty; v3.29.1 shipped 5 files) — they moved to a
    separate `crd.projectcalico.org.v1` chart. Symptom: the helm prime fails with
    *"no matches for kind Installation / APIServer / Goldmane / Whisker in
    version operator.tigera.io/v1 — ensure CRDs are installed first."* This is an
    **install-contract change, not a bad pin**, and it applies to any Calico
    ≥3.32. Resolved by the new `gitops/crds/` tier + a server-side-apply task in
    `bootstrap-cluster.yml`. **The obvious fix — a second HelmRelease with
    `dependsOn` — does NOT work**: 3 of the 32 CRDs exceed the 262144-byte
    client-side apply limit, so they require server-side apply, which
    helm-controller (and `helm install`) can't do. Full reasoning, the vendoring
    decision, and the `prune: false` rationale: `gitops/CLAUDE.md` "The CRD
    tier". **On every `calico_version` bump, regenerate `gitops/crds/calico/
    crds.yaml` in the same commit** — see item 20, which proposes dropping Helm
    for Calico entirely and makes this simpler.

    **✅ DECIDED 2026-08-02 — pin `v3.32.1` (latest stable) AND pre-apply the
    #12890 RBAC workaround.** An earlier draft of this item recommended the
    `v3.31.x` line; that is **superseded**. Release landscape at decision time:
    **v3.32.1** (2026-06-26) is the latest *stable* release —
    `repos/projectcalico/calico/releases/latest` returns it, and that endpoint
    excludes prereleases by definition. `v3.33.0-0.dev` exists but **there is no
    v3.33.0 and no RC**; Calico opens every line with a `-0.dev` tag.

    **Why latest, with a known bug, rather than the safe line:**
    - **The bug is fully understood, not a mystery.** PR
      [#11839](https://github.com/projectcalico/calico/pull/11839) — *"Fix
      ipamconfigs -> ipamconfigurations"*, milestone **Calico v3.32.0** — renamed
      the ClusterRole to `ipamconfigurations` while the deployed CRD is still
      `ipamconfigs`. That single PR explains the whole thing, including why 3.31
      works: **the rename isn't in 3.31.** A half-finished CRD migration, not a
      flake.
    - **The failure is loud, immediate, and pre-empted.** With the workaround
      applied up front we never hit it. If it somehow fires, the signature is
      unmistakable (LB IPs `pending`, one specific `ipam.go` log line).
    - **3.32 pairs with k8s 1.36**, which item 16 bumps to anyway — so this is
      one rebuild instead of two, and the version-matching objection disappears.
    - **⚠ `v3.32.1` does NOT contain a fix.** Confirmed: the only RBAC change on
      `release-v3.32` since the report is an unrelated kubevirt grant (#12996).
      Do not assume a patch release resolved it — the workaround is **required**,
      not precautionary.

    **The workaround — pre-applied, not reactive.** A `ClusterRole` +
    `ClusterRoleBinding` granting the `calico-kube-controllers` SA access to
    `ipamconfigs`, in **both** API groups (`projectcalico.org` and
    `crd.projectcalico.org` — the error is on the latter, but the confirmed-working
    version grants both):

    ```yaml
    # gitops/infrastructure/calico/kube-controllers-ipamconfigs-rbac.yaml
    # WORKAROUND for projectcalico/calico#12890 — see §7 item 15. REMOVE when fixed.
    apiVersion: rbac.authorization.k8s.io/v1
    kind: ClusterRole
    metadata:
      name: calico-kube-controllers-ipamconfigs-workaround
    rules:
      - apiGroups: [projectcalico.org, crd.projectcalico.org]
        resources: [ipamconfigs]
        verbs: [get, list, create, update, delete, watch]
    ---
    apiVersion: rbac.authorization.k8s.io/v1
    kind: ClusterRoleBinding
    metadata:
      name: calico-kube-controllers-ipamconfigs-workaround
    roleRef:
      apiGroup: rbac.authorization.k8s.io
      kind: ClusterRole
      name: calico-kube-controllers-ipamconfigs-workaround
    subjects:
      - kind: ServiceAccount
        name: calico-kube-controllers
        namespace: calico-system
    ```

    - **Apply it with the Calico prime (Ansible), not just via Flux.** LB
      allocation is needed early in the Flux chain (Gateway → cert-manager → ESO
      all sit behind a LoadBalancer IP), so a gap here stalls the bootstrap.
      Priming it alongside Calico closes the gap and is idempotent.
    - **The verb list is broader than the observed failure requires** — the log
      only shows a failed `get`. It's kept as posted because that's the
      *confirmed-working* set, and inventing a narrower untested variant during a
      CNI migration is the wrong time to economise. Tighten to `get,list,watch`
      later if desired, as its own change.
    - **🔁 REMOVAL CRITERIA — re-check on every Calico bump.** This is deliberate
      cruft with an expiry. Drop it once either (a) the shipped
      `calico-kube-controllers` ClusterRole grants `ipamconfigs`, or (b) the CRD
      is actually renamed to `ipamconfigurations` so upstream's grant matches.
      Check with the assertion below *after removing* — if it still says `yes`,
      upstream fixed it and the workaround can go.
    - **We are now walking through the one-way gate** we previously avoided: v3.32
      requires migrating to **ClusterNetworkPolicy** (Admin/Baseline Admin Network
      Policies are replaced). **Verified a no-op for us — no policies exist yet.**
      Consequence for future work: the pod-isolation enforcement discussed in §6
      step 5 should use Calico `GlobalNetworkPolicy` / `ClusterNetworkPolicy`,
      never the deprecated AdminNetworkPolicy path.

    **Verification — now an assertion, not a probe.** After the prime, this must
    return `yes`:
    ```
    kubectl auth can-i get ipamconfigs \
      --as=system:serviceaccount:calico-system:calico-kube-controllers
    ```
    A `no` means the workaround didn't land, and LB IPs will sit `pending` while
    **BGP advertisement looks perfectly healthy** — routes *are* advertised, so
    the BGP side gives no hint at all. Worth an explicit assert task in the
    bootstrap play rather than a manual check. **Contingency if LB IPAM still
    misbehaves for some other reason:** MetalLB's *controller* only (speaker
    disabled), which Calico's own 3.29 docs recommend — the dataplane decision
    stands regardless, and no MetalLB speaker ever ships.

    **Mechanics when bumping:** `calico_version` in `group_vars/all/vars.yml` and
    the `version:` in `gitops/infrastructure/calico/helmrelease.yaml` must move in
    lockstep (both Renovate-tracked). **Renovate may track the 3.32 line**, but
    every bump must re-run the removal check above — a fix landing upstream is the
    good outcome and we should notice it. Also worth a
    look before touching anything: [projectcalico#9457](https://github.com/projectcalico/calico/issues/9457),
    titled "VXLanCrossSubnet issue on v3.29" — our exact current version *and*
    mode. Title only; not yet read.

16. **⚠ k3s is on an EOL Kubernetes — bump it with the Calico work (raised
    2026-08-02).** We run **k3s v1.32.3**. Upstream Kubernetes maintains release
    branches for **the most recent three minors only — currently 1.36, 1.35,
    1.34** — so 1.32 is two minors below the oldest supported. This isn't
    "would be nice to be newer"; we're on an unsupported Kubernetes.
    - **1.36 is current stable** (released 2026-04-22), *not* pre-release.
      **1.37 is due 2026-08-26** — three weeks out at time of writing. Don't
      chase 1.37: Calico support for it won't land until ~3.33, and being ahead
      of your CNI is worse than being one behind. Landing on 1.36 now means
      being a normal, supported N-1 once 1.37 ships.
    - **No provisioning gate.** The Flatcar sysext bakery publishes k3s transfer
      configs for **v1.32 → v1.36** (verified 2026-08-02 via
      `gh api repos/flatcar/sysext-bakery/releases/tags/k3s`). The `.raw` images
      themselves resolve at download time through the `MatchPattern` in
      `k3s-sysupdate.conf.j2`, so a minor bump is just `k3s_minor` +
      `k3s_version` in `group_vars`.
    - **The four-minor jump costs nothing here — do NOT plan sequential
      upgrades.** Kubernetes only supports one-minor-at-a-time for *in-place*
      upgrades, but this cluster is empty and §1 proved from-scratch rebuild
      works. So we re-provision at the target minor rather than walking
      1.32→1.33→1.34→1.35→1.36. This is exactly the payoff of the immutable
      provisioning built in §1–2; it evaporates the moment there's real state.
    - **Decision is coupled to item 15's Calico line, and the coupling is the
      whole point.** Three candidate pairings:

      | k3s | Calico | Verdict |
      |---|---|---|
      | **1.36** | **3.32.1** | **✅ CHOSEN.** Matched pair, both latest stable; #12890 pre-empted by the workaround in item 15. |
      | 1.36 | 3.31 | Would need 3.31↔1.36 support confirmed; moot now. |
      | 1.35 | 3.31 | Attested working in #12890's thread; the conservative option we passed on. |

      **✅ DECIDED 2026-08-02 — k3s `v1.36.x` + Calico `v3.32.1`.** Both are
      current stable and they're the version-matched pair (3.32 "corresponds with
      Kubernetes v1.36"), so the pairing objection that drove the earlier 3.31
      recommendation is gone. The #12890 risk is accepted knowingly and
      **neutralised up front** by pre-applying the RBAC workaround — see item 15,
      including its removal criteria.

      Do this in the *same* rebuild as the Calico encapsulation change (§6 step
      5); they're both re-provisions, so it's one disruption instead of two.
      **Note 1.37 ships 2026-08-26** — do not chase it. Calico support won't land
      until ~3.33, and being ahead of the CNI is worse than being one behind;
      1.36 becomes a normal supported N-1 at that point.
    - **Consequence for §2's seed-vs-running note:** bumping `k3s_minor` re-points
      sysupdate at the new minor's transfer config. Expect the seeded version and
      the running version to re-converge at rebuild, then drift again within the
      new minor — same behavior as documented, new baseline.

17. **Calico eBPF dataplane — ✅ DONE 2026-08-03, BOTH STAGES, committed.**
    (Decided 2026-08-02; trialled, verified and made durable 2026-08-03.)
    Reasoning record, verification detail and revert procedure:
    `docs/calico-ebpf-single-node-trial.md`.
    - **Result: source IP preserved under `externalTrafficPolicy: Cluster`.** The
      pod's observed `RemoteAddr` went from the node's address to the real
      off-cluster client on the flip, policy unchanged. **Stage 1 re-verified on
      a from-scratch rebuild from committed config** — the hand-patches that set
      it up died with the previous node, so the repo owns the result.
      **Stage 2** then removed kube-proxy entirely.
    - **⚠ Verifying stage 2 needs care — the obvious check is worthless.** k3s
      *embeds* kube-proxy and never had a DaemonSet, so `get ds kube-proxy` →
      NotFound proves nothing; and eBPF handles packets first, so the source-IP
      test passes either way. What proved it: no `*proxy*` process in
      `/proc/*/comm`, `:10249` not listening, **0** residual `KUBE-SERVICES`
      chains, and `felix/kube-proxy.go` live in the reconcile loop. **`:10256`
      IS listening and that's correct** — Felix's `bpfKubeProxyHealthzPort`
      default, deliberately on kube-proxy's port. Don't read it as failure.
    - **`felixconfiguration.yaml` was deleted at stage 2, deliberately.** It only
      disabled `bpfKubeProxyIptablesCleanupEnabled` for coexistence; with
      kube-proxy gone, leaving cleanup off would ORPHAN its stale iptables rules.
      The 0-chain count confirms Felix cleaned up. Don't reintroduce it without
      also reverting `disable-kube-proxy` in the k3s config.
    - **DSR still NOT enabled**, per the original decision — it remains a
      one-line runtime `FelixConfiguration` patch, no re-provision needed.
    Original decision and reasoning, unchanged and still the justification:
    - **The reason is source IP, not performance.** With `externalTrafficPolicy:
      Cluster`, kube-proxy SNATs externally-originated traffic to the node IP and
      the client address is gone before the pod sees it — unrecoverable at L7, so
      NGF would log node IPs as clients forever. Calico's eBPF dataplane preserves
      it under `Cluster`. That **collapses the four-row matrix** in
      `docs/pfsense-frr-bgp-setup.md` §10 to one row and deletes the
      `Local`-vs-`Cluster` trade entirely. The CPU/latency win from dropping
      kube-proxy is real but negligible at our scale — **do not let it drive
      this.**
    - **⚠ DSR is explicitly OUT.** Source IP preservation comes from eBPF mode in
      the default `Tunnel` mode; `bpfExternalServiceMode: DSR` only optimises the
      return path and in exchange requires the fabric to let nodes emit packets
      sourced from each other's IPs. New requirement, rounding-error payoff.
    - **Staged, because the cost asymmetry is large and separable.** Stage 1
      (`linuxDataplane: BPF` + `bpfKubeProxyIptablesCleanupEnabled: false`,
      kube-proxy left running) reverts with one `kubectl patch`, touches no
      Ignition, and buys the source IP. Stage 2 (`--disable-kube-proxy`) costs a
      **re-provision** because the k3s config comes from Ignition, and buys only
      efficiency. **Stopping permanently after stage 1 is a legitimate outcome.**
      If stage 2 happens at all, fold it into the same rebuild as item 16.
    - **Reversibility is the reason this is cheap** — and the structural
      difference from an eBPF-only CNI. Calico's dataplane is a switch
      (`linuxDataplane: Iptables` reverts it, documented and supported), policy
      semantics / CRDs / IPAM / BIRD all unchanged. ⚠ The switch **is disruptive
      to existing connections in both directions**, which is harmless today and
      won't be later — an argument for running it *now*. What genuinely does not
      revert: the debugging toolchain. `iptables-save` stops telling you anything
      and it's `calico-node -bpf` instead.
    - **Independent of the BGP work, in both directions.** eBPF replaces
      kube-proxy, not routing — BIRD still carries BGP and the `frr.conf` in the
      runbook is byte-identical either way. The stage-1 test runs against a
      **NodePort**, so it needs no LB IP and no FRR session and could run today.
      Neither task blocks the other.
    - **Preconditions verified live on `snoop-a2o` 2026-08-02**, not assumed:
      kernel `6.12.95-flatcar` (needs ≥5.10), `/sys/fs/cgroup` cgroup2 `rw`,
      `/run` writable tmpfs, **bpffs and debugfs already mounted** (so
      `mount-bpffs` has nothing to fail at).
      **⚠ Do NOT copy Talos cgroup guidance here.** The widely-repeated advice to
      override `CALICO_CGROUP_PATH` / `cgroupV2Path` on "immutable OSes" is
      [#7892](https://github.com/projectcalico/calico/issues/7892), which is
      Talos-specific — Talos's rootfs is read-only except `/var`, whereas Flatcar
      makes only `/usr` read-only and `/run` is an ordinary systemd tmpfs.
      Verified empirically above. Keep `cgroupV2Path` as a **diagnostic** (it's
      available on the 3.32.1 pin), never as a pre-emptive setting.
      *General lesson: Talos and Flatcar get lumped together as "immutable" and
      their writable surfaces are nothing alike — same class of assumption as the
      `raw`-module rule in §8.*
    - **Two historical eBPF bugs are already fixed at our pin**, which is part of
      why now is reasonable: the eBPF-vs-iptables tail-latency regression (eBPF
      conntrack reclaimed faster than the kernel's `TIME_WAIT` → spurious RSTs,
      hundred-ms p99s) fixed in **3.30**; and `bpfin.cali`/`bpfout.cali` stuck at
      MTU 1500 under a jumbo underlay
      ([#8868](https://github.com/projectcalico/calico/issues/8868), closed by
      PR #8922) — which matters here specifically because of `mtu 8996` on eth1.
    - **⚠ Two traps that make this fail confusingly rather than loudly:**
      (a) the `kubernetes-services-endpoint` ConfigMap must carry the node's
      **real IP, never `localhost`** — [#9141](https://github.com/projectcalico/calico/issues/9141)
      is `kube-controllers` dying on `dial tcp [::1]:6443` while `calico-node` and
      `typha` come up fine, i.e. a *partial* failure; (b) leaving
      `bpfKubeProxyIptablesCleanupEnabled` at its default while kube-proxy still
      runs makes Felix and kube-proxy overwrite each other's iptables rules on
      repeat.
    - **What one node cannot prove — say this whenever the result is cited.** The
      VXLAN tunnel path only runs when the backend is on a *different* node, so
      it's never exercised (and that's exactly where the tail-latency bug lived);
      ECMP / `maximum-paths` / the node-to-node mesh need ≥2 nodes. And **mixed
      eBPF and standard-dataplane nodes are unsupported**, so node 2's join is a
      *cluster-wide* dataplane flip, not a per-node rollout — single-node testing
      structurally hides that.
    - **Repo integration** (nothing applied by hand once decided):
      `linuxDataplane: BPF` goes in `gitops/infrastructure/calico/values.yaml`
      next to the queued `bgp: Enabled` + no-encap change, so the Ansible prime
      and the Flux `configMapGenerator` stay byte-identical. The endpoint
      ConfigMap is **Ansible-primed** (needed before the dataplane works, so it
      can't wait for Flux) and carries a node IP → it's topology, committed as
      `${k3s_api_ip}` and resolved via `postBuild.substituteFrom`, never a
      literal. `${k3s_api_ip}` is **derived, not a new vault var**:
      `{{ dmz_network.subnet_base }}.{{ node_number }}` from `inventory/nodes.yml`.

18. **⚠ Snippet-dir permissions are NOT durable host prep — and the gap silently
    blocked every worker node (hit and fixed 2026-08-02).** Provisioning died on
    `Destination /mnt/pve/cephfs/snippets not writable`. Root cause: the dir was
    `root:root 0755` instead of `drwxrws--- root pve-snippets` — README "Proxmox
    SSH access" step 4's `chgrp`/`chmod` half wasn't in effect, while the
    `groupadd`/`usermod` half was (so `id` looked correct and the dir existed).
    - **Why it hid for a month.** Ansible's `copy` checks the *directory* for
      writability **only when the destination file doesn't exist**; otherwise it
      checks the file. `snoop-a2o.ign` was already there from the §1 run, so the
      directory permission was never exercised. Deleting that file didn't create
      the bug — it removed the thing masking it.
    - **⚠ It was a standing blocker for §6 step 2, not a one-off.** The
      destination is `{{ proxmox_snippet_dir }}/{{ hostname }}.ign` — **every new
      node writes a new filename**, so the first worker would have hit this
      regardless. §1's "verified end-to-end" never actually covered it.
    - **⚠ It RECURS — this is not a one-off, and detection alone was not enough.**
      **Observed twice, 2026-08-02 and 2026-08-03.** PVE recreates storage
      content subdirectories as `root:root 0755` on **storage activation**, and
      *a template rebuild is enough to trigger it* (`qm destroy` + image import
      touch storage). The second occurrence was caught by the assert added after
      the first — which proved the mechanism but also proved that asserting only
      buys you a manual root `chgrp` every time it happens, discovered when a
      provision fails. The giveaway that it's this and not something else: the
      storage root and `snippets/` share an mtime, because both were recreated;
      deleting a file inside would touch only the child.
    - **✅ RESOLVED 2026-08-03 — the role now self-repairs.** `flatcar_vm` stats
      the dir, repairs group+setgid when it isn't writable, re-stats, then still
      asserts. Repair needs root, so **two fixed-argument sudoers rules** were
      added alongside `qm` (README step 3), installed and verified on **all three
      PVE nodes**: positive tests pass, and negative tests confirm the rules
      can't chmod another path or chgrp another group. `pve-snippets` is **gid
      1001 on all three** — worth having checked, since the dir lives on shared
      CephFS which stores the numeric gid, so a mismatched group id on one node
      would have failed only when Ansible happened to target that node.
      - **The design premise changed, not just the config.** The README used to
        argue `qm` was the *only* root command needed because the snippet write
        was handled by owning the dir. Owning the dir turned out not to be
        durable, so that rationale was rewritten rather than patched.
      - **⚠ The sudoers rules are per-node and fixed-argument.** Adding them on
        one host and forgetting the others fails only when provisioning targets
        the missed node. Changing `vault_proxmox_snippet_dir`,
        `proxmox_snippet_group`, or `proxmox_snippet_mode` means updating
        `/etc/sudoers.d/provisioner` on **every** PVE node, or the repair is
        silently refused.
      - The assert stays as the backstop for exactly those cases. The original
        `file: state=directory` check passed happily against `root:root 0755` —
        *existence was never the thing in doubt.*
      - **✅ Repair path verified end-to-end 2026-08-03** (third occurrence, this
        time self-healed): `stat` → **repair `changed`** on both sudo commands →
        re-`stat` → assert `ok` → upload `changed`, with no human involved. The
        resulting file is `-rw-rw---- provisioner pve-snippets`, which
        simultaneously confirms the `0660` join-token fix and that the setgid
        group inheritance works in practice.
      - **⚠ …but that verification covered only the REPAIR path, and the HAPPY
        path was broken (found + fixed 2026-08-03, during item 21).** The re-stat
        reused `register: snippet_dir`, and **a skipped task still registers** —
        as `{changed, skipped, skip_reason}`, with no `stat` key. So whenever the
        dir needed *no* repair, the skipped re-stat overwrote the good stat and
        the assert failed `exists=n/a` against a perfectly healthy directory.
        **The polarity is what hid it: it passed exactly when the dir was broken
        and failed exactly when it was fine**, so the end-to-end check above —
        run on an occurrence — could only ever see the passing case. Fixed by
        registering the re-stat as `snippet_dir_repaired` and having the assert
        take `snippet_dir_repaired.stat | default(snippet_dir.stat)`. Now
        verified on **both** paths. Lesson worth keeping: *a fix verified only on
        the failure it was written for is half-verified* — the branch where the
        problem is absent is a distinct path.
      - **The reset is cheaper to trigger than "a template rebuild" implies.**
        This occurrence followed merely deleting the snippet. The storage root's
        mtime changed a few minutes *before* the repair while `snippets/` changed
        at repair time — and a parent's mtime only moves when an entry in it is
        created or removed, so the content subdirectories were genuinely
        recreated. Assume **any** storage-touching operation can do it; that's
        the case for self-repair over a documented manual fix.
    - **Second fix, same class — security-relevant.** The upload was
      `mode: "0644"`, and that `.ign` **embeds the k3s cluster join token**
      (`k3s-config.yaml.j2` → `token:`), which per the root `CLAUDE.md` lets
      anyone holding it register a node. It was world-readable, protected only by
      the dir's `2770` — the exact state we just watched revert to `0755`. Now
      `0660`, so exposure takes two independent regressions instead of one.

19. **Flatcar OS update policy is unset by default — decide it before there's
    state (raised 2026-08-02).** There is **no `update-engine`/`locksmith`
    configuration anywhere** in the Ignition templates (grepped `roles/` +
    `playbooks/`), so Flatcar's default auto-update-and-reboot is in force. §1.4's
    DoD deliberately proved unattended reboot works — that same mechanism now
    means **nodes move to new stable on their own schedule**.
    - **Consequence for testing:** you cannot pin the tested OS by controlling
      the template; the node walks away from it. That matters most for §7 item
      17 — **eBPF behavior is kernel-dependent**, so an unattended reboot into a
      new kernel mid-trial reads as a flake, and a "verified" result may be
      against a kernel you're no longer running. **Record OS + kernel with the
      trial result** (`4593.2.4` / `6.12.95-flatcar` as of 2026-08-02).
    - **Related trap — the template is a pin by accident.** `flatcar_version:
      "current"` means a *rebuild* always fetches latest stable, but
      `flatcar_template`'s build is guarded on `qm status <vmid>` failing, so
      `build-template.yml` **runs green and silently skips** whenever vmid 9000
      exists. A successful run is not evidence of a fresh template. Rebuilding =
      `qm destroy 9000` then re-run (safe: clones are `full: true`, so existing
      VMs are independent). A stale template also means every provision boots
      old, then auto-updates and reboots shortly after — the mid-trial reboot,
      near-scheduled.
    - **Open question, not yet decided:** leave the default (fine while
      disposable, and why §1.4 tested it), or move to a k8s-aware
      drain-then-reboot. It stops being fine once there's Ceph-backed state on a
      single control-plane node (item 10). **Don't just set `reboot-strategy:
      off`** — that trades unplanned reboots for unpatched nodes.
    - Cheap mitigation available now: add a `flatcar_template_force` flag so a
      rebuild is `-e flatcar_template_force=true` rather than a manual `qm
      destroy`, and so the silent-skip stops being a trap.

20. **Drop Helm for Calico and install the operator from manifests — PROPOSED
    2026-08-02, do it AFTER the current rebuild verifies.** Investigated while
    fixing the v3.32 CRD split (item 15). Calico supports a manifest install
    alongside the chart; neither is deprecated.
    - **The sizes make the case.** `manifests/tigera-operator.yaml` is **19.6 KB**
      — Namespace, ServiceAccount, 2 ClusterRoles, bindings, one Deployment.
      `manifests/operator-crds.yaml` is the 32 CRDs and is **byte-for-byte
      identical content** to what `helm template crd.projectcalico.org.v1`
      produces (verified: 40,019 lines each, `diff` clean), so `gitops/crds/`
      needs **no rework** — regeneration just becomes a `curl`.
    - **We use nothing the chart provides.** `bgp`, `ipPools`,
      `nodeAddressAutodetectionV4` all live in the **`Installation` CR**; the
      chart's only job is passing `installation:` straight through to it.
    - **What it deletes:** `helmrepository.yaml`, `helmrelease.yaml`, the
      `configMapGenerator` + `disableNameSuffixHash` + `valuesFrom` indirection,
      the `helm` binary as a control-node prerequisite, `kubernetes.core.helm`,
      the Helm-4-vs-`kubernetes.core` 6.5 compatibility pin in
      `requirements.yml`, and helm-controller from Calico's dependency chain.
    - **The big one: it dissolves the adoption problem.** "Ansible primes, Flux
      adopts" is delicate — release name, namespace, and chart version must match
      exactly or helm-controller fights the primed release. With manifests there
      is no release identity to match; both sides server-side apply the same YAML,
      idempotent by construction. **And the HelmRelease is the only thing in the
      repo that needs adoption** (`BGPPeer` and the #12890 workaround are already
      plain Ansible-primed manifests; everything after Flux exists needs no
      priming). So this removes the concept entirely rather than leaving a special
      case — **`gitops/CLAUDE.md`'s "reference example of the handoff" framing
      would need rewriting, not patching.**
    - **What we'd give up:** chart knobs for the *operator deployment* (registry,
      pull secrets, tolerations, resources) become kustomize patches — rare here;
      and Renovate ergonomics, since a chart `version:` is first-class while a
      vendored manifest needs a regex manager on a pinned version string (already
      true for `crds.yaml` regardless).
    - **⚠ Sequencing: not now.** The bootstrap was only just unblocked and the
      1.36/3.32.1 rebuild is unverified. Landing this on top puts two variables in
      flight — the same attribution argument accepted for item 17's staging. It's
      also *cheaper* afterwards, with a known-good cluster to diff against.

21. **✅ DONE 2026-08-03 — destroy the Ignition snippet after first boot; it holds
    the k3s join token (raised and closed the same day).** The `.ign` on the shared snippet
    storage embeds `token:` (`k3s-config.yaml.j2`), and **Ignition reads it
    exactly once** — the `ignition.firstboot` flag is cleared afterwards, so past
    first boot it's a live credential with no remaining purpose, sitting on
    CephFS reachable from all three hypervisors. The token's other two copies
    (the vault, and `/etc/rancher/k3s/config.yaml` on the node) are both
    necessary; this is the one that isn't, and it has the widest blast radius.
    - **Agreed shape (Chris, 2026-08-03): do it in `provision-nodes.yml`, not
      `bootstrap-cluster.yml`.** Add up-checks to the provisioning play — the
      same pattern bootstrap already uses — then clean up there. Better than the
      alternatives considered: it keeps a Proxmox concern in the Proxmox
      playbook, the play already owns the VM lifecycle (so it owns `cicustom`),
      and it stays correct for a node that is provisioned but never bootstrapped.
    - **The up-check that matters is SSH on :22**, which is a genuine
      Ignition-completed signal rather than a proxy for one — the admin user and
      its `authorized_keys` come *from* Ignition, so port 22 answering means the
      config was consumed. `bootstrap-cluster.yml` already does exactly this
      ("Wait for SSH (port 22) on each server's DMZ IP"); reuse it.
    - **⚠ ORDER IS LOAD-BEARING, and getting it wrong breaks the boot path.**
      Remove `cicustom` from the VM config FIRST, THEN delete the snippet. **PVE
      rebuilds the cloud-init config drive on every VM start**, and
      `read_cloudinit_snippets_file` ends in
      `PVE::Tools::file_get_contents($full_path, ...)` with **no error handling**
      (verified in `/usr/share/perl5/PVE/QemuServer/Cloudinit.pm`, 2026-08-03).
      Delete the file while `cicustom` still points at it and `qm start` dies —
      not at the next provision, but at **the next reboot**, which is the worst
      time to find out.
      - **Detach goes over the API (`proxmox_kvm` `delete: cicustom`), not `qm
        set` over SSH** — the role already *attaches* it that way, so the pair is
        symmetric and the scoped sudo stays untouched (root `CLAUDE.md`: prefer
        the API, keep SSH to the snippet file itself). The file *delete* is a
        filesystem op, so it stays on SSH like the upload — and needs no sudo:
        unlinking needs write on the setgid dir, which is the same grant the
        upload uses.
      - **The `proxmox_vm_info` read before the detach is there for the
        wrong-VM assert, first and foremost.** `flatcar_vm` sets `hostname`/
        `vmid`/`eth0_ip` as *facts*, and **facts outrank task vars** — so a
        cleanup task reusing those names would silently get the LAST provisioned
        node's values and detach `cicustom` from someone else's VM. Every var in
        `destroy-ignition-snippet.yml` is `cleanup_*`-prefixed for that reason,
        and the assert (`exactly one VM, and its name == this node`) is the
        backstop. Secondarily: `proxmox_kvm` hard-codes `changed=True` on
        `delete`, so gating on `config.cicustom` keeps the task honest — though
        in the normal flow the role re-attaches `cicustom` a few tasks earlier,
        so it nearly always *does* have something to detach.
      - **✅ Safe on a RUNNING VM — verified two ways in the PVE 9.2.3 source,
        because "it lands in `[PENDING]`" would have made the ordering argument
        above worthless.** (1) Every key of `$confdesc_cloudinit` — `cicustom`
        included — is added to `$fast_plug_option` (`QemuServer.pm`), so
        `vmconfig_hotplug_pending` applies the delete to the **live config
        immediately** rather than deferring it. (2) Even if it *were* deferred,
        `vm_start_nolock` applies pending changes **and reloads the config**
        before calling `apply_cloudinit_config`, so the drive can never be
        regenerated against a stale `cicustom`. Confirmed empirically too: after
        the detach, `qm config --current 0` shows nothing pending.
    - **⚠ Backup/restore trap — documented in `ansible/README.md`.** Restoring a
      VM backup taken *before* the cleanup yields a config that still has
      `cicustom` pointing at a snippet that no longer exists → the VM won't
      start. Recovery is re-running provisioning for that node (or
      `qm set <vmid> --delete cicustom` by hand), but that's something to know
      during an actual restore rather than derive at 2am.
    - **✅ `ide2` STAYS — verified inert across a reboot, not assumed.** Keeping
      it is also the *correct* choice rather than merely the cheap one: a
      cloud-init drive is standard on any PVE cloud-init VM, the template ships
      `--ide2 <storage>:cloudinit` so every clone gets one back, and `cicustom`
      needs it to land on at the next rebuild — removing it would be a hardware
      change re-done on every rebuild, forever. What PVE generates for it with
      `cicustom` gone carries nothing sensitive (`qm cloudinit dump 1050 user`:
      `hostname`/`fqdn`/`users: default`/`package_upgrade` — no `sshkeys`, no
      `cipassword`, because none are ever set on these VMs). The credential was
      in the *snippet*, never in the drive.
      - **Reboot evidence (`qm reboot 1050`, a full stop→start so PVE really
        regenerates the drive — a guest-side `reboot` would not test this):**
        `qm reboot` exits 0 (the ordering test itself — a stale `cicustom` dies
        exactly here), SSH answers in ~10 s, node **Ready**, all pods
        Running/Completed. Journal shows `ignition-subsequent.target — Subsequent
        (Not Ignition) boot complete` and `ignition-delete-config.service ...
        skipped (ConditionFirstBoot=true)` — Ignition provably did **not** re-run.
        `sr0` shows up labelled `cidata` but **mounted nowhere**.
      - **Afterburn was the one real candidate, and it's clean.**
        `coreos-metadata-sshkeys@core.service` *does* run every boot and did
        rewrite `/home/core/.ssh/authorized_keys` — but its only source is
        `authorized_keys.d/ignition` (from the original Ignition run); no
        `coreos-metadata` source file appeared, matching the empty `sshkeys` in
        the dump. The `admin` user, whose keys we actually SSH with, is untouched.
    - **Minor cost, accepted knowingly:** the on-storage record of "what config did
      this node actually get" goes away. It's reproducible from the vault + node
      map, so this is a debugging convenience, not data — and
      `destroy_ignition_snippet: false` buys it back when debugging a first boot
      (that flag also skips the up-check, so the play won't block on a node that
      never comes up).
    - **Also landed with it:** the play now asserts `node_filter` matched
      something (a typo used to provision nothing, silently — worse now that the
      same filter drives cleanup), and one node failing to boot no longer strands
      the *other* nodes' tokens: cleanup runs for everything that came up, then
      the play fails naming what didn't, leaving those `.ign`s deliberately in
      place (deleting them with `cicustom` still attached is the trap above).

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
