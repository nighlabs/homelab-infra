# Preparing the Proxmox Ceph cluster for ceph-csi

Creates the Ceph-side objects Kubernetes needs — one RBD pool, one CephFS
subvolumegroup, two restricted cephx users — on the **existing Proxmox Ceph
cluster**. The Kubernetes side (the operator, the drivers, the StorageClasses)
lives in `gitops/`; this runbook is only the storage cluster.

The decisions this runbook implements, with the alternatives rejected:

- reuse the Proxmox Ceph, never a second Ceph in-cluster; two StorageClasses;
  a dedicated pool and a restricted client —
  [ADR-0006](decisions/0006-ceph-csi-external-proxmox-ceph.md)
- the second vNIC on the Ceph public VLAN, tagged, jumbo, no default route —
  [ADR-0017](decisions/0017-static-addressing-no-dhcp.md)
- credentials live in 1Password and are read at run time;
  nothing secret is committed in any form —
  [ADR-0034](decisions/0034-secrets-store-1password.md)

**The scripts are generated** by `ansible/playbooks/render-ceph-setup.yml` from
`inventory/group_vars/all/vars.yml` + 1Password — don't hand-write or edit them. See
§8.

> **⚠ This runs as root on a live production Ceph cluster.** That cluster
> already serves Proxmox VM storage. Every step below is **additive**: a new
> pool, a new subvolumegroup, two new cephx users. Nothing writes to an existing
> pool, touches the CephFS root, or modifies an existing cephx user. Read §2 and
> §6 before starting.

**Blinding rule:** this document is committed, so it contains **no real
addresses, pool names, or filesystem names from the existing cluster.** Values
appear as `${placeholder}` matching the 1Password field names
(`ansible/SECRETS.md`). Names *we* create are shown literally — they're
ours, generic, and reveal nothing. Do not paste real values back into this file.

---

## 1. Values

### Ours — cleartext, in `group_vars/all/vars.yml` under `ceph_csi`

These name objects that **do not exist yet**. We choose them, they're generic,
and keeping them readable is what makes this runbook followable.

| Value | Default | What it is |
|---|---|---|
| `rbd_pool` | `k8s-rbd` | New RBD pool for Kubernetes RWO volumes |
| `subvolume_group` | `k8s` | Subvolumegroup inside the **existing** CephFS → `/volumes/k8s` |
| `rbd_client` | `k8s-rbd` | cephx entity `client.k8s-rbd` |
| `cephfs_client` | `k8s-cephfs` | cephx entity `client.k8s-cephfs` |
| `namespace` | `ceph-csi` | Kubernetes namespace for the operator and drivers |

### Theirs — vaulted, describes the cluster that already exists

| 1Password field | What it is |
|---|---|
| `ceph_fs_name` | PVE's existing CephFS name. **Prerequisite** — the render fails without it |
| `ceph_mons` | Mon addresses, **one per line**, on the Ceph **public** network |
| `ceph_fsid` | Cluster uuid from `ceph fsid`; doubles as ceph-csi's `clusterID` |
| `ceph_k8s_rbd_key` 🔑 | cephx key for `client.k8s-rbd` |
| `ceph_k8s_cephfs_key` 🔑 | cephx key for `client.k8s-cephfs` |

**Only `ceph_fs_name` is needed up front.** The other four are *produced* by
this runbook — `ceph-k8s-keys.sh` prints them in §5. The render checks for them
softly and says so, rather than making a first run impossible.

> ⚠ **`ceph_mons` must be public-network addresses.** The Ceph *cluster*
> (replication) network is untagged on the same bond as the public VLAN, so an
> address from it looks perfectly plausible and routes nowhere from a k3s node.
> The render asserts every mon is inside `${ceph_subnet_base}`; read them off
> `ceph mon dump`, not off a host's interface list.

---

## 2. What this creates — and what it deliberately does not

**Creates:**

| Object | Notes |
|---|---|
| RBD pool `k8s-rbd` | `size 3`, `min_size 2`, autoscaler on — matching the cluster's existing pools |
| Subvolumegroup `k8s` | Inside `${ceph_fs_name}`, at `/volumes/k8s` |
| `client.k8s-rbd` | Caps scoped to the new pool |
| `client.k8s-cephfs` | Caps scoped to that filesystem and that path |

**Deliberately does not:**

- **Register the pool as a PVE storage.** If Proxmox knew about `k8s-rbd` it
  could schedule VM disks into it, and a pool the two tiers share is exactly
  what ADR-0006's "dedicated pool" is for. Both the setup and verify scripts
  treat "registered in `pvesm status`" as a **failure**, not a warning.
- **Create a second CephFS.** The chosen layout joins PVE's existing filesystem
  under its own subvolumegroup. A second filesystem needs a second **active**
  MDS; with three MDS daemons (one active, two standby) that spends the standby
  redundancy PVE's production filesystem relies on. The gain — pool-level
  isolation — is already delivered for the RWO path by the separate RBD pool,
  which is where the real data goes.
- **Modify an existing cephx user.** `ceph auth get-or-create` is idempotent for
  an identical cap set and errors on a different one. The script leaves an
  existing user's caps alone and tells you how to change them deliberately. A
  silent cap change is how you get a driver that works until it doesn't.
- **Touch PVE's VM pool** — the RBD pool holding live VM images — in any way.
  It has no placeholder here because nothing in this repo ever names it; the
  guardrail is that our pool is a *different* pool and our caps are scoped to
  ours.

---

## 3. The caps, and why each one

### `client.k8s-rbd`

```
mon "profile rbd"
osd "profile rbd pool=k8s-rbd"
mgr "profile rbd pool=k8s-rbd"
```

`profile rbd` on **mon** carries `allow command "osd blocklist"`. That isn't
incidental — ceph-csi needs to blocklist a dead node's client before
re-attaching its image elsewhere, which is the exact path ADR-0006's
Non-Graceful Node Shutdown handling depends on. Remove it and RWO failover
hangs instead of failing over.

Both `osd` and `mgr` are pinned to our pool, so this credential cannot read or
write PVE's VM images even if it leaks.

### `client.k8s-cephfs`

```
mon "allow r"
mgr "allow rw"
osd "allow rw tag cephfs metadata=${ceph_fs_name}, allow rw tag cephfs data=${ceph_fs_name}"
mds "allow rw path=/volumes/k8s"
```

> ⚠ **`mgr "allow rw"` is broader than everything else here, and it is not an
> oversight.** Subvolume create/delete/resize all run through the mgr `volumes`
> module, and Ceph ships no narrower profile for it. This single cap is the
> reason the two users are **split at all**: it means a CephFS-side compromise
> is meaningfully worse than an RBD-side one, so the RBD credential — the one
> guarding Postgres, Qdrant and Redis — must not carry it.

The `mds` cap is scoped to our subvolumegroup path, so this user cannot read
PVE's snippets, ISOs or backups, which live outside `/volumes` entirely.

**If a CephFS volume operation ever fails with `EACCES`,** widening the mds cap
from `path=/volumes/k8s` to `path=/volumes` is the first thing to try — some
subvolume operations touch the group's parent. Widen it deliberately and record
why; don't reach for `allow *`.

---

## 4. Procedure

### 4.1 Prerequisite: `ceph_fs_name` in 1Password

On a Proxmox node:

```sh
ceph fs ls
```

Store the filesystem's name in 1Password as `ceph_fs_name`, in the project the
control node's machine account reads (`ansible/SECRETS.md`).

### 4.2 Render

```sh
cd ansible
uv run ansible-playbook playbooks/render-ceph-setup.yml
```

Writes three git-ignored files to `ansible/.ceph/` (§8). The play touches
nothing — it reads inventory and 1Password and writes three local files. It does not
connect to Proxmox, to Ceph, or to any node.

### 4.3 Read the script

Not optional. `ceph-k8s-setup.sh` is about to run as root against the storage
underneath every VM in the house. It is ~150 lines, mostly guards, and you
should be able to say what each `ceph` invocation does before running it.

### 4.4 Copy and run

```sh
scp ansible/.ceph/ceph-k8s-*.sh <pve-node>:/tmp/
ssh <pve-node>
sudo bash /tmp/ceph-k8s-setup.sh
```

Run it on **one** node — any mon host. Ceph state is cluster-wide; running it
on each node in turn does nothing extra and only widens the window for a
half-applied change.

The script refuses to start if the cluster is `HEALTH_ERR`, and prompts before
continuing on `HEALTH_WARN`. Both are deliberate: don't stack a storage change
on top of a cluster that's already unhappy.

### 4.5 Verify

```sh
sudo bash /tmp/ceph-k8s-verify.sh
```

Read-only, prints no keys, exits non-zero on any failure. It checks the pool's
`size`/`min_size`/application tag, that the pool is **not** a PVE storage, that
the subvolumegroup exists, and that every cap on both users matches what
`gitops/` is about to assume — including a negative check that the RBD user's
osd cap is pool-scoped with no wildcard.

Re-runnable forever. This is the "is the storage tier still shaped right" check
to reach for when something breaks in a year.

### 4.6 Collect the keys

```sh
sudo bash /tmp/ceph-k8s-keys.sh
```

> ⚠ **This prints two cephx keys to your terminal.** That's the point — they're
> generated *on* the Ceph cluster, and this is how they reach 1Password, the only
> durable store for them. Afterwards: clear your scrollback, and close the
> terminal if it logs to disk.

It prints four values under their exact 1Password field names: `ceph_fsid`,
`ceph_mons`, `ceph_k8s_rbd_key`, `ceph_k8s_cephfs_key`. Store each in 1Password.

### 4.7 Clean up the PVE node

```sh
rm -f /tmp/ceph-k8s-*.sh
```

They carry no key, but they're generated artifacts and a PVE node is not where
they live.

### 4.8 Seed the cluster

Back on the control node — this is what puts the keys into Kubernetes as
`Secret`s, in the bootstrap-secret tier:

```sh
cd ansible
uv run ansible-playbook playbooks/bootstrap-cluster.yml
```

---

## 5. Rotation

Rotating a cephx key is a **two-place** change, and the order matters:

```sh
# On a PVE node — mints a new key for the SAME entity, caps unchanged.
ceph auth get-or-create-key client.k8s-rbd
```

That invalidates the old key immediately. Update 1Password, then re-seed and restart
the drivers, or in-flight mounts start failing authentication.

> ⚠ **Never `ceph auth del` a user that's in use.** Every mounted volume on
> every node authenticates as it — deleting it doesn't just stop new volumes,
> it breaks the ones already attached.

---

## 6. Rollback

There is no teardown script, deliberately: every destructive Ceph operation
here is one typo away from a production pool, and a script makes that typo
cheap to run. To undo, by hand, in this order:

1. Delete the Kubernetes objects first (`ceph-csi` Kustomizations, then any PVCs
   using these StorageClasses). Deleting Ceph objects underneath live PVCs
   strands volumes rather than freeing them.
2. `ceph auth del client.k8s-rbd` / `ceph auth del client.k8s-cephfs` — only
   once nothing is mounted.
3. `ceph fs subvolumegroup rm ${ceph_fs_name} k8s` — fails while subvolumes
   exist, which is the safety you want.
4. The RBD pool last, and only if you're certain: pool deletion is irreversible
   and requires `mon_allow_pool_delete`, which on a PVE cluster is normally
   off. **Leaving an empty pool costs nothing.** Prefer that.

---

## 7. Verifying from the Kubernetes side

The Ceph side being right doesn't prove the *path* is right. Both were
confirmed before the first PVC (worklog):

- All three mons reachable from a node on the Ceph public NIC, on msgr1
  (`6789`) and msgr2 (`3300`).
- Jumbo frames end-to-end: an 8968-byte payload with DF set arrives unfragmented
  (8968 + 28 = 8996). ADR-0006 warns that jumbo must be set at **both** the
  Proxmox NIC definition and the guest networkd unit or the link silently caps
  at 1500 — this is the test that catches it.
- `rbd.ko`, `ceph.ko` and `libceph.ko` all present in the Flatcar kernel.

> ⚠ **Don't test TCP reachability from a Flatcar node with bash's `/dev/tcp`.**
> Flatcar's bash is built without net-redirections, so `/dev/tcp/host/port` does
> not exist and *every* port reads as closed — including ports that are open.
> This produces a convincing false negative. Use `curl telnet://host:port` with
> a known-open port as a control.

---

## 8. Repo integration

### The scripts are generated, not hand-written

```sh
cd ansible
uv run ansible-playbook playbooks/render-ceph-setup.yml
```

Writes three git-ignored files to `ansible/.ceph/`:

| File | What it does |
|---|---|
| `ceph-k8s-setup.sh` | §4.4 — creates the pool, subvolumegroup and two users |
| `ceph-k8s-verify.sh` | §4.5 — read-only; confirms caps match what `gitops/` assumes |
| `ceph-k8s-keys.sh` | §4.6 — prints the keys + fsid + mons for 1Password |

Same generate-then-deliver-by-hand pattern as
[`pfsense-frr-bgp-setup.md`](pfsense-frr-bgp-setup.md), and for the same reason:
the target is live production gear with no safe automation path, so the
*content* is versioned and asserted even though the *delivery* is manual.

Here the missing automation path is concrete: the `ceph` CLI reads
`/etc/pve/ceph.conf` out of pmxcfs (root-only), the repo's `provisioner` SSH
user has sudo scoped to `qm`, and **cephx user creation is not in the Proxmox
API at all**. Widening a standing privilege on a production Ceph cluster, for a
step that runs approximately once, is the worse trade.

> ⚠ **Ceph is a render target, not a source of truth** — the same trap as
> pfSense. Changing a cap with `ceph auth caps` and not updating the template
> means the next render silently disagrees with the cluster, and
> `ceph-k8s-verify.sh` is what catches it. Whatever you change on the cluster,
> change in the template too.

**Committed:** `playbooks/render-ceph-setup.yml`,
`playbooks/templates/ceph-k8s-{setup,verify,keys}.sh.j2`, the `ceph_csi` block
in `group_vars/all/vars.yml`.
**Never committed:** anything under `ansible/.ceph/` — git-ignored, `0600` in a
`0700` directory.

### What the play asserts before rendering

- `ceph_fs_name` is in 1Password (the one hard prerequisite)
- object names match `^[a-zA-Z0-9][a-zA-Z0-9._-]*$`, and the two client names
  differ — they're separate cephx entities, which is the whole point
- the RBD pool name doesn't collide with `<fs>_data` / `<fs>_metadata` / `.mgr`
- **if present:** every mon is on the Ceph public network, and the fsid is a
  36-character uuid

It can't see the cluster's real pool list, so the collision check is
re-done **on** the cluster by the setup script, where the truth is.

---

## 9. Troubleshooting

| Symptom | Cause |
|---|---|
| `Error initializing cluster client: ... error calling conf_read_file` | Not root. `/etc/pve/ceph.conf` lives in pmxcfs and is root-only — this is also why the repo's `provisioner` user can't run these |
| Render fails: `ceph_fs_name` is not in 1Password | §4.1 — the one prerequisite |
| Render fails: mon addresses not on the Ceph public network | You read them off the cluster/replication network. Use `ceph mon dump` |
| Setup refuses: pool is registered as a PVE storage | The name collides with a real PVE storage. Pick another `rbd_pool`; never share a pool with PVE |
| Setup refuses: pool exists but is not tagged for rbd | The name collides with something else real. Investigate before renaming |
| CephFS volume op fails `EACCES` | Try widening the mds cap to `path=/volumes` (§3), deliberately |
| Everything is right but volumes won't mount | Check the *path*, not the caps — §7. Jumbo MTU and the Ceph public NIC are the usual culprits |
