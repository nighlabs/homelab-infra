# ansible/ — Flatcar VM provisioning

Current scope: build a **rebuildable** Flatcar VM shell — two NICs, a separate
data disk, key-only SSH — via Ignition delivered through Proxmox's config-drive
(`cicustom`); bake **k3s** in via the Flatcar k3s sysext; then wait for k3s to
boot and prime **Calico** so the node goes Ready; then **bootstrap Flux**, which
adopts that primed Calico and takes ownership of the cluster (verified live
2026-08-29). See `ansible/CLAUDE.md` (§1 shell, §2 k3s, §6 Calico/Flux) for the
definitions of done.

## Layout

```
ansible.cfg              # inventory/roles/library paths, BWS notes
requirements.yml         # community.proxmox collection (+ butane on PATH)
inventory/
  hosts.yml              # localhost + the PVE host (SSH, for snippet/qm)
  nodes.yml              # node map — SOURCE OF TRUTH for node identity/addressing
  group_vars/            # adjacent to the inventory so it loads for every playbook
    all/                 # a DIRECTORY -> every file loads for group `all`
      vars.yml           # structure + {{ bws.* }} refs (nothing sensitive)
BWS-SECRETS.md           # WHAT TO CREATE IN BITWARDEN — the secret manifest
library/
  bws_secrets.py         # bulk BWS fetch (one API call, not one per secret)
roles/
  flatcar_template/      # download proxmoxve image -> import -> template (idempotent)
  flatcar_vm/            # render Butane -> Ignition, clone, pin MACs, disk, cicustom, boot
playbooks/
  build-template.yml     # build the Flatcar template
  provision-nodes.yml    # provision every node in nodes.yml
  bootstrap-cluster.yml  # wait for k3s, fetch kubeconfig, prime Calico + BGP
  flux-bootstrap.yml     # install Flux; it then adopts the primed Calico
site.yml                 # all four, in dependency order
```

## Control-node prerequisites

Everything the control node needs, in one place (the single source of truth —
`pyproject.toml`, `requirements.yml`, and the setup steps below just point here).

**External binaries** — install yourself; not managed by uv or ansible-galaxy:

| Binary | Used by | Required? |
|---|---|---|
| `uv` | bootstraps the Python venv below | **yes** — install first |
| `butane` | `flatcar_vm` role — Butane→Ignition transpile (+ a `butane --version` preflight) | **yes** (provisioning) |
| `helm` | `bootstrap-cluster.yml` — Calico prime via `kubernetes.core.helm` | **yes** (bootstrap) |
| `kubectl` | the "Verify" steps only — **no play invokes it** (the `k3s kubectl` readyz check runs on the *node*) | recommended |
| `ssh`, `git` | ansible transport / cloning this repo | baseline |

Get the Go binaries from their upstream releases. **Note the Helm version:**
`kubernetes.core` shells out to `helm` and parses its output, so it gates on the
major — **Helm 4 needs `kubernetes.core` >= 6.4** (the 5.x line hard-fails with
"Helm version must be >=3.0.0,<4.0.0"). `requirements.yml` pins 6.x for exactly
this. If a future Helm major breaks the Calico prime, `helm_binary` in
`group_vars/all/vars.yml` pins a specific binary (see that file).

**Python packages** — pulled by `uv sync` from `pyproject.toml`/`uv.lock`:
`ansible-core` (provides `ansible-playbook` / `-galaxy` / `-vault`), `proxmoxer`,
`requests`, `kubernetes`, `bitwarden-sdk`.

**Ansible collections** — pulled by `ansible-galaxy … -r requirements.yml` into
the in-repo `.ansible/`: `community.proxmox`, `kubernetes.core`.

*Not* needed on the control node: `qm` / `qemu-img` / image tooling (those run on
the PVE host or inside Ignition), and anything k8s-side on the Flatcar nodes.

## One-time setup

The control-node Python toolchain is managed with [uv] — it owns the interpreter,
the venv, and the lockfile (contents listed under **Control-node prerequisites**
above). Run these from the **repo root** (where `pyproject.toml`/`uv.lock` live);
the rest from `ansible/`.

1. `uv sync` — creates `.venv` with the exact pinned deps from `uv.lock`
   (installs Python 3.12 automatically if you don't have it).
2. `uv run ansible-galaxy collection install -r requirements.yml` — installs
   `community.proxmox` into the in-repo `.ansible/` path (isolated, like the venv).
3. Install the **external binaries** from **Control-node prerequisites** above
   (`butane`, `helm`; `kubectl` recommended) — they're not pip/uv-managed.
4. **Set up Bitwarden Secrets Manager — see [`BWS-SECRETS.md`](BWS-SECRETS.md).**
   That file is the complete manifest: the project + read-only machine account
   to create, the access token to put in your macOS **Keychain**, `BWS_ORG_ID`,
   and every secret name with its expected format. It includes the Proxmox API
   credential (create it per **[Proxmox API token & user](#proxmox-api-token--user-one-time-on-a-pve-node)**
   below) and the environment specifics (IPs, subnets/VLANs, gateways,
   hostnames, storage names, SSH public key).

   **There is no `vault.yml` and no vault passphrase.** Secrets are fetched at
   run time in a single API call; secret zero is the Keychain item. Why:
   `docs/mac-studio-inference-stack-2.md`, Appendix A, "Control-node secrets".
5. `inventory/group_vars/all/vars.yml` needs no editing for secrets — it's just
   structure plus `{{ bws.* }}` references. Only generic, non-revealing
   defaults (MAC OUI, Flatcar channel/version) remain in cleartext there.
6. `inventory/hosts.yml` needs no editing — the PVE host's address and login
   user resolve from BWS too.

## Proxmox API token & user (one-time, on a PVE node)

The provisioning uses a **scoped API token** (`ansible@pve!provisioning`),
deliberately **not** `root@pam` (design doc Appendix A). Run these as `root` on
any node — `pveum` is cluster-wide:

```bash
# 1. Dedicated user in the 'pve' realm (token-only, no password needed)
pveum user add ansible@pve

# 2. Least-privilege role for VM lifecycle + disk + storage allocation.
#    NB: no 'VM.Monitor' — it's not a valid privilege and isn't needed.
pveum role add Provisioning -privs \
  "VM.Allocate VM.Clone VM.Config.Disk VM.Config.CPU VM.Config.Memory \
   VM.Config.Network VM.Config.Options VM.Config.HWType VM.Config.Cloudinit \
   VM.Config.CDROM VM.PowerMgmt VM.Audit \
   Datastore.Audit Datastore.AllocateSpace \
   SDN.Use"

# 3. Grant it on / with propagation
pveum acl modify / -user ansible@pve -role Provisioning

# 4. Mint the token. --privsep 0 makes the token inherit the user's perms
#    (with privsep ON — the default — the token has an EMPTY ACL and you get
#    403s despite the role above; that's the #1 gotcha).
pveum user token add ansible@pve provisioning --privsep 0
```

Step 4 prints the token **value** exactly once — copy it into the
`proxmox_api_token_secret` secret in BWS. It can't be retrieved later; if lost, delete
and recreate the token.

**Fuss-free alternative:** skip the custom role (steps 2–3) and grant the
built-in `PVEVMAdmin` role instead — it bundles the `VM.*` privileges plus
`Datastore.AllocateSpace`/`Datastore.Audit`. Slightly broader than least-
privilege, fine for a homelab:

```bash
pveum acl modify / -user ansible@pve -role PVEVMAdmin
```

Verify the token authenticates (swap in your host + secret):

```bash
curl -sk -H "Authorization: PVEAPIToken=ansible@pve!provisioning=<secret>" \
  https://<pve-host>:8006/api2/json/version
# -> {"data":{"version":...}} means it works
```

Notes:
- `SDN.Use` is required on **Proxmox 8.x** even for plain Linux bridges
  (`vmbr0`/`vmbr1`): PVE 8 represents them under an auto-generated SDN zone
  (`localnetwork`), so assigning a guest NIC to a bridge — which the clone does
  when it copies the template's `net0` — checks `SDN.Use` on
  `/sdn/zones/localnetwork/<bridge>`. Without it the clone fails with
  `403 Forbidden: Permission check failed (…, SDN.Use)`. It's in the role privs
  above; to add it to an already-created role:
  `pveum role modify Provisioning --privs "SDN.Use" --append`.
- This token is only the **API** half. Attaching *and detaching* `cicustom` both
  go through it (`proxmox_kvm`), but the snippet **upload and delete** are file
  operations on the storage, so they go over **SSH** — see below.

## Proxmox SSH access — `provisioner` user (one-time, per PVE node)

Some steps can't go through the API — placing the Ignition snippet on the
storage (Proxmox has no snippet-upload API) and the `qm` calls (its CLI is
root-only, ignoring the token/ACL system). So the control node also needs SSH
to the PVE host. Rather than logging in as `root`, use a dedicated
`provisioner` user with **sudo scoped to just `qm`**. Run on **each node you
provision from** (here, `phoenix-1`), as root:

```bash
apt-get install -y sudo          # PVE is minimal Debian; sudo may be absent

# 1. Dedicated login user
useradd -m -s /bin/bash provisioner

# 2. Authorize your control-node public key
install -d -m700 -o provisioner -g provisioner /home/provisioner/.ssh
# paste your ~/.ssh/id_ed25519.pub into:
#   /home/provisioner/.ssh/authorized_keys   (chown provisioner:provisioner, chmod 600)

# 3. Passwordless sudo, SCOPED to fixed commands (never ALL) — the whole point.
#    qm    : root-only CLI, ignores the API token/ACL system.
#    chgrp } repair the snippets dir when PVE resets it (see the ⚠ below step 4).
#    chmod } Fully-qualified paths with FIXED arguments — sudo matches the entire
#            command line, so these cannot touch another path or group.
#    ⚠ Validate BEFORE installing; a broken sudoers file is painful to undo.
cat >/tmp/prov.sudoers <<'SUDOERS'
provisioner ALL=(root) NOPASSWD: /usr/sbin/qm, /usr/bin/chgrp pve-snippets /mnt/pve/cephfs/snippets, /usr/bin/chmod 2770 /mnt/pve/cephfs/snippets
SUDOERS
visudo -cf /tmp/prov.sudoers && install -m 440 -o root -g root /tmp/prov.sudoers /etc/sudoers.d/provisioner
rm -f /tmp/prov.sudoers
#    ⚠ PER-NODE, and every node that Ansible might SSH to needs it — adding it on
#    one host and forgetting the others produces a failure that only shows up
#    when provisioning happens to target the node you missed.
#    ⚠ The paths/group/mode must match `proxmox_snippet_*` in
#    inventory/group_vars/all/vars.yml byte-for-byte, or the repair is refused.

# 4. Let provisioner write the Ignition snippet WITHOUT sudo (keeps the sudoers
#    to just `qm`). Proxmox references snippets by a FLAT volid
#    (`<storage>:snippets/<file>` — no subdirectories), so the file must land
#    directly in the snippets dir (= the proxmox_snippet_dir secret). Keep that dir
#    root-owned and give provisioner write via a shared group — no extra
#    package, and works regardless of whether CephFS has ACL support:
mkdir -p /mnt/pve/cephfs/snippets            # ensure it exists (root:root)
groupadd -f pve-snippets
usermod  -aG pve-snippets provisioner        # applies on provisioner's next login
chgrp pve-snippets /mnt/pve/cephfs/snippets
chmod 2770 /mnt/pve/cephfs/snippets          # setgid: new files inherit the group; root+group only
ls -ld /mnt/pve/cephfs/snippets              # verify: drwxrws--- root pve-snippets
#    Alternative (POSIX ACL) if you prefer per-user grants — needs the `acl`
#    package AND a CephFS mounted with ACL support (else setfacl fails with
#    "Operation not supported"; fall back to the group approach above):
#      apt-get install -y acl
#      setfacl -m u:provisioner:rwx -d -m u:provisioner:rwx /mnt/pve/cephfs/snippets
```

This keeps the snippets dir `root:root` — provisioner gets write access, not
ownership. (A provisioner-owned *subdirectory* won't work: cicustom can't point
at `snippets/<subdir>/<file>`.)

> **⚠ Step 4 is not durable state — PVE resets it, and `flatcar_vm` now repairs
> it automatically.** PVE recreates storage content subdirectories as
> `root:root 0755` on storage activation, silently undoing the `chgrp`/`chmod`
> while leaving the group membership from steps 2–3 intact — so `id` looks
> correct and the directory still exists, but the write fails. **Observed twice
> (2026-08-02 and 2026-08-03); a template rebuild is enough to trigger it**,
> since `qm destroy` + image import touch storage.
>
> Because it recurs, detection isn't enough: `flatcar_vm` stats the directory,
> **repairs it via the scoped `chgrp`/`chmod` sudoers rules from step 3**, then
> re-stats and asserts. A missed node or a changed path shows up as the assert
> firing with the manual fix in the message, rather than as `copy` failing four
> tasks later with a message naming neither the group nor this README.
>
> Manual repair, if you need it (needs **root** — the sudoers rules are
> fixed-argument and won't cover a different path):
>
> ```bash
> chgrp pve-snippets /mnt/pve/cephfs/snippets
> chmod 2770 /mnt/pve/cephfs/snippets
> ls -ld /mnt/pve/cephfs/snippets     # want: drwxrws--- root pve-snippets
> ```
>
> Note the failure only reproduces when the target `<hostname>.ign` doesn't
> already exist — Ansible's `copy` checks the *directory* only in that branch —
> so a leftover snippet hides it until the next new node. That is why it went
> unnoticed until a rebuild, and why it was a standing blocker for worker nodes.

Then set the `proxmox_ssh_user` secret to `provisioner` in BWS (the default in
`BWS-SECRETS.md`). The roles invoke `qm` via the `proxmox_qm` helper
(`inventory/group_vars/all/vars.yml`), which resolves to `sudo qm` for a
non-root user and plain `qm` for root — so it works either way.

Verify it end-to-end:

```bash
ssh provisioner@<pve-host> 'sudo qm list'   # scoped sudo works, no password
uv run ansible proxmox -m raw -a 'sudo qm list'
```

With this in place you can set `PermitRootLogin no` in the PVE host's `sshd`
and never SSH as root for provisioning.

- **Why scoped and not `NOPASSWD: ALL`?** The roles need exactly three root
  commands, so they get exactly three. `qm` is root-only by design; `chgrp` and
  `chmod` exist solely to repair the snippets dir after PVE resets it, and are
  pinned to one group, one mode and one path. Because sudo matches the whole
  command line, none of them can be repurposed — that property is the point, and
  it's why they're spelled out rather than given a wildcard.
  - The *snippet write itself* still needs no sudo — that's what owning the dir
    via the group (step 4) buys. The added rules restore that ownership when the
    storage layer takes it away; they don't replace it.
  - If you later add a role step shelling out to another root-only tool (e.g.
    `pvesm`), extend the sudoers line the same way: full path, fixed arguments,
    `visudo -cf` before installing, and **on every PVE node**.

## Run

Prefix ansible commands with `uv run` so they use the pinned venv (or activate it
once with `source .venv/bin/activate` and drop the prefix):

```bash
uv run ansible-playbook site.yml              # template + nodes + Calico + Flux
uv run ansible-playbook playbooks/provision-nodes.yml \
    -e node_filter=snoop-a2o                  # just provision one node
uv run ansible-playbook playbooks/bootstrap-cluster.yml \
                                              # wait for k3s + prime Calico
uv run ansible-playbook playbooks/flux-bootstrap.yml \
                                              # install Flux, hand over the cluster
```

`site.yml` runs all four in order: build the template → provision the node
shells (k3s bakes in via Ignition) → `bootstrap-cluster.yml` waits for k3s to
boot, fetches the kubeconfig to `ansible/.kube/<cluster>.config` (git-ignored),
and primes **Calico** so the node goes Ready → `flux-bootstrap.yml` installs Flux,
which *adopts* that same Calico release. The pinned definition lives in
`../gitops/infrastructure/calico/`.

`flux-bootstrap.yml` is the last thing Ansible does to a cluster; after it,
**changes arrive through Git, not through Ansible**. It helm-installs the
flux-operator, applies one `FluxInstance` CR, and waits for the three committed
entrypoint Kustomizations (`crds` → `infrastructure` → `apps`) to go Ready.

Two things about it that are easy to trip over:

- It **requires `bootstrap-cluster.yml` to have run first** — it needs that play's
  kubeconfig *and* its `cluster-topology` Secret. Both are asserted with messages
  that say so, but it can't recover either on its own: this play never touches a
  node.
- It needs **no credentials at all** — no BWS, no keychain prompt. Everything it
  uses is a committed constant or comes from the cluster via the kubeconfig.

`bootstrap-cluster.yml` is **per-cluster**: it elects one bootstrap primary per
cluster in `inventory/nodes.yml` and primes each cluster's Calico against that
cluster's own kubeconfig. Only one cluster exists today, so the multi-cluster path
is structurally in place but untested against real hardware.

### The Ignition snippet is destroyed after first boot

`provision-nodes.yml` doesn't stop at "VM is running". It waits for **SSH on port
22** for every node it provisioned, and then, for each node that answered,
removes `cicustom` from the VM config and deletes `<hostname>.ign` from the
snippet storage.

Port 22 is a genuine *Ignition-completed* signal rather than a proxy for one —
the admin user and its `authorized_keys` come **from** Ignition, so sshd
answering means the config was consumed. And once it has been, the snippet is
dead weight of the worst kind: it embeds the **k3s join token**, Ignition reads
it exactly once (`ignition.firstboot` is cleared on that boot), and it sits on
storage every hypervisor in the cluster can reach. The token's other two copies —
BWS, and `/etc/rancher/k3s/config.yaml` on the node — are both load-bearing;
this one isn't.

> **⚠ The order is load-bearing: `cicustom` comes off the VM config BEFORE the
> file is deleted.** PVE regenerates the cloud-init drive on *every* VM start,
> and `read_cloudinit_snippets_file` ends in `file_get_contents` with no error
> handling — so a snippet deleted while `cicustom` still points at it doesn't
> fail at provision time, it fails at the node's **next reboot**, with `qm start`
> dying. `playbooks/tasks/destroy-ignition-snippet.yml` enforces the order and
> documents why it's safe to do while the VM is running.

> **⚠ Restore trap.** A VM backup taken *before* the cleanup restores a config
> that still references a snippet which no longer exists — **the VM won't
> start**. Recovery is just re-running `provision-nodes.yml` for that node (it
> re-uploads the snippet and re-sets `cicustom`), but it's worth knowing during a
> restore rather than deriving it at 2am. Alternatively, clear the stale
> reference by hand: `qm set <vmid> --delete cicustom`.

The `ide2` cloud-init drive **stays attached** — that's normal for any Proxmox
cloud-init VM, the template ships it, and `cicustom` needs it to land on at the
next rebuild. With `cicustom` gone PVE generates a *default* cloud-init config
for it, which is inert on Flatcar (Ignition is firstboot-only) and carries
nothing sensitive: no `ciuser`/`cipassword`/`sshkeys` are ever set on these VMs.

| Var | Default | Effect |
|---|---|---|
| `destroy_ignition_snippet` | `true` | Run the post-boot cleanup. Set `false` to keep the snippets — this also skips the SSH up-check, so the play won't block on a node that never boots. The escape hatch while debugging a first boot. |
| `node_boot_timeout` | `300` | Seconds to wait for a node's SSH before treating it as failed to boot. |

If a node doesn't come up in time, the play **still cleans up every node that
did**, then fails naming the ones it couldn't — their `.ign` is deliberately left
in place (deleting it with `cicustom` still attached is the trap above). Fix the
boot, re-run the play, and the cleanup finishes.

### Kubeconfig handling

k3s emits a kubeconfig whose cluster, user, **and** context are all named
`default`, pointing at `127.0.0.1`. Two clusters built this way collide on every
entry name and silently overwrite each other, so `bootstrap-cluster.yml` rewrites
it on fetch, keyed off the **cluster name — which is the cluster key in
`inventory/nodes.yml`**, not a separate setting:

- entries renamed → cluster `homelab`, user `homelab-admin`, context `homelab`;
- `server:` repointed from `127.0.0.1` to the node's **DMZ IP** so the control
  node (and, next milestone, Flux) can reach the API;
- written `0600` to **`ansible/.kube/<cluster>.config`** — one file per cluster
  (a shared path would have each bootstrap clobber the last). These are the
  canonical files the plays themselves use.

Each cluster is then **merged** into your personal `~/.kube/config`
(`kubeconfig_merge_user: true`) via `kubernetes.core.kubeconfig`, so plain
`kubectl --context homelab` works with no `KUBECONFIG` juggling. It's a merge of
that cluster's three named entries, never a whole-file overwrite — every other
context is left untouched (including your other clusters'), and re-running is
idempotent. Knobs, all in `group_vars/all/vars.yml`:

| Var | Default | Effect |
|---|---|---|
| `kubeconfig_merge_user` | `true` | Merge into `~/.kube/config`. Set `false` on CI/shared control nodes. |
| `kubeconfig_user_path` | `$HOME/.kube/config` | Which file to merge into. |
| `kubeconfig_set_current_context` | `true` | Whether the merge also makes the cluster kubectl's *active* context. Set `false` once a second cluster exists — otherwise they each claim it in turn and the last one bootstrapped wins. |

To rename the context, rename the cluster key in `inventory/nodes.yml`. Doing it
*after* a bootstrap leaves the old entries behind in `~/.kube/config` and an
orphaned `ansible/.kube/<old>.config` — both are yours to delete.

[uv]: https://docs.astral.sh/uv/

## Adding a node

Add one entry under the cluster's `nodes:` in `inventory/nodes.yml` with a unique
`node_number` (1..254); the DMZ IP (`<dmz_subnet_base>.<n>`), Ceph-public IP
(`<ceph_subnet_base>.<n>`), MACs, and `vmid` (1000+n) are all derived from it
(subnet bases come from BWS).

`node_number` and hostname uniqueness is **global — across every cluster**, not
per-cluster: all clusters share the DMZ/Ceph subnets and the Proxmox vmid space,
so reusing a number in a second cluster collides on both an IP and a vmid. Both
are asserted (`playbooks/tasks/load-node-map.yml`) before anything is created.

## Adding a cluster

Add another key under `clusters:` in `inventory/nodes.yml` with its own `nodes:`
and non-overlapping `node_number`s. Nothing else in the repo changes: the cluster
key becomes its kubeconfig context and `ansible/.kube/<cluster>.config`, and
`bootstrap-cluster.yml` elects that cluster its own bootstrap primary and primes
its own Calico. Set `kubeconfig_set_current_context: false` at that point (see
above). Untested against real hardware — only one cluster exists today.

## Verify (definition of done)

After boot, over SSH to the node's DMZ IP (`<dmz_subnet_base>.<n>`):

- `ip a` — static addresses on both NICs; `eth1` MTU **8996**, not 1500
- `ip link show` — NIC MACs match the derived `<mac_oui>:00:<n hex>:0{0,1}`
- `ip route show dev eth1` — only the connected subnet, **no default route**
- `resolvectl status` / `getent hosts <name>` — DNS via the resolver from BWS
- `hostnamectl` — matches the node map key
- `df -h` / `mount` — data disk (vdb) mounted at `/var/lib/rancher` (k3s's
  default data-dir root; k3s state lands here, off the OS disk)
- `sudo reboot` with no console → comes back identical
- delete the VM, re-run the play → identical MAC/IP/hostname (real rebuild test)

### k3s (nodes with a k3s `role`, e.g. `all-in-one`)

The k3s server is baked into Ignition via the Flatcar k3s sysext — no manual
post-boot steps. The ~50 MB sysext image is **downloaded on first boot** by
`k3s-sysext-download.service` (Ignition can't fetch it in the initramfs — no
DHCP; see `ansible/CLAUDE.md` §2), so k3s is up **~30–60 s after boot**, not
instantly. The node is **NotReady until Calico** (its CNI arrives later via
Flux); that's expected here, not a failure. Over SSH to the DMZ IP:

- `systemctl status k3s-sysext-download.service` → **active (exited)** on first
  boot (skipped/`condition` on later boots, once the `.raw` is cached)
- `systemd-sysext status` lists **k3s**; `/usr/local/bin/k3s --version` matches
  the pin for that node's cluster (`k3s_version_default` in
  `group_vars/all/vars.yml`, or a `k3s_version:` override on the cluster in
  `inventory/nodes.yml`)
- `systemctl is-active k3s` → **active**; `journalctl -u k3s` clean
- `sudo k3s kubectl get nodes -o wide` → the node present, version = pin,
  **`STATUS NotReady` (expected — no CNI)**, `INTERNAL-IP` = the eth0/DMZ IP
  (never eth1)
- `mount | grep /var/lib/rancher` — vdb is mounted at `/var/lib/rancher`, so the
  datastore + embedded containerd (`/var/lib/rancher/k3s/{server,agent}`) live on
  the **data disk**, not the OS disk — with k3s using its stock default data-dir
- `sudo k3s secrets-encrypt status` → **Enabled**, aescbc (on from boot 1). No
  `--data-dir` needed — the datastore is at the default path (that's the point of
  mounting vdb at `/var/lib/rancher` rather than overriding `data-dir`).
- `sudo k3s kubectl get pods -A` → **no** traefik/servicelb/local-path; coredns
  + metrics-server present but **Pending** (no CNI yet)
- node `.spec.podCIDR` inside `10.42.0.0/16`; the API cert carries the tls-san
  entries (`echo | openssl s_client -connect <dmz-ip>:6443 2>/dev/null |
  openssl x509 -noout -text | grep -A1 'Subject Alternative'`)
- `systemctl list-timers systemd-sysupdate.timer` — active; `systemd-sysupdate
  -C k3s-<minor> list` tracks the `k3s-<minor>.@v` pattern (actual patch
  pull-through is a separate PoC — `ansible/CLAUDE.md` §7 item 5)
- delete the VM, re-run the play → k3s comes up **identically** from Ignition
  (the real rebuild test, same as the shell above)

### Calico / cluster bootstrap (`bootstrap-cluster.yml`)

After `bootstrap-cluster.yml` (or the full `site.yml`) runs, the node should flip
from NotReady to **Ready**. From the control node, using the fetched kubeconfig:

- `ansible/.kube/homelab.config` exists (mode 0600) — named for the cluster key in
  `inventory/nodes.yml` — its `server:` is the node's **DMZ IP**, not
  `127.0.0.1`, and its cluster/user/context are named `homelab` / `homelab-admin`
  / `homelab`, **not** k3s's `default`
  (`grep -E 'server:|name:' ansible/.kube/homelab.config`)
- `kubectl config get-contexts` → the `homelab` context present in
  `~/.kube/config`, alongside any contexts you already had (the merge preserves
  them). Skip this one if you set `kubeconfig_merge_user: false`.
- `kubectl --context homelab get nodes -o wide` → **Ready**, `INTERNAL-IP` = the
  eth0/DMZ IP
- `kubectl get installation default -o jsonpath='{.spec.calicoNetwork.ipPools[0].cidr}'`
  → `10.42.0.0/16` (matches `k3s_cluster_cidr`)
- `kubectl get pods -n tigera-operator` and `-n calico-system` → all **Running**
- `kubectl get pods -A` → coredns + metrics-server now **Running** (were Pending
  pre-CNI)
- `helm -n tigera-operator list` → release `tigera-operator`, chart version =
  `calico_version` — this is the release Flux adopts next milestone
- **Idempotency:** re-run `bootstrap-cluster.yml` → no changes (release present,
  node already Ready)

### BGP / LoadBalancer

The pfSense side must already be configured and parked —
`docs/pfsense-frr-bgp-setup.md`. It needs **no change for a rebuild**: the node's
address derives from `node_number`, so a destroy-and-recreate comes back on the
same IP and pfSense's `neighbor` line stays valid.

⚠ **Session state is not proof of anything.** Both of this design's silent
failure modes — RFC 8212 policy refusal and a prefix list without `le 32` —
present as a perfectly healthy `Established` session with nothing flowing. The
last two checks are the ones that actually matter.

From the control node:

```sh
# Dataplane is no-encap and BGP is on
kubectl get ippool default-ipv4-ippool -o jsonpath='{.spec.ipipMode}{" "}{.spec.vxlanMode}'   # Never Never
kubectl get installation default -o jsonpath='{.spec.calicoNetwork.bgp}'                      # Enabled

# The CRs were primed
kubectl get bgpconfiguration default -o jsonpath='{.spec.asNumber}'      # the cluster ASN
kubectl get bgppeer pfsense -o jsonpath='{.spec.peerIP}{" "}{.spec.asNumber}'
kubectl get bgpfilter pfsense-lb-only -o yaml | grep -A4 exportV4
kubectl get ippool loadbalancer-pool -o jsonpath='{.spec.allowedUses}'   # ["LoadBalancer"]

# #12890 workaround landed — a `no` here means every LB IP will sit pending
# while BGP looks perfectly healthy
kubectl auth can-i get ipamconfigs \
  --as=system:serviceaccount:calico-system:calico-kube-controllers        # yes
```

On pfSense (`vtysh`, see the runbook §7):

- `show bgp summary` → the node **`Established`**, not `Active`/`Connect`
- `show ip bgp` → contains the LB range and **no pod CIDR**. If `10.42.0.0/16`
  appears, *both* the `BGPFilter` and pfSense's inbound prefix list failed.
- `show ip bgp neighbors <node ip> advertised-routes` → **empty** (we advertise
  nothing to the cluster)

**The two checks that actually prove it works** — a throwaway Service:

```sh
kubectl create deploy bgptest --image=nginx --port=80
kubectl expose deploy bgptest --type=LoadBalancer --port=80

# 1. ALLOCATION (Calico LB IPAM + the #12890 workaround)
kubectl get svc bgptest -w        # EXTERNAL-IP leaves <pending> for one in the LB range

# 2. REACHABILITY (BGP + the prefix lists + the firewall rules)
curl -sS http://<that IP>/        # from a DIFFERENT segment, not the node

kubectl delete deploy/bgptest svc/bgptest
```

`pending` forever = allocation (suspect #12890 or the pool). Allocated but
unreachable = advertisement, filtering, or a missing firewall rule to the LB
range — learning a route and being permitted to use it are different things
(runbook §6).

### Flux (`flux-bootstrap.yml`)

The play asserts most of this itself and fails with the reason; these are for
checking a cluster by hand, or for working out *why* it failed.

```sh
# The operator, then the instance it manages
kubectl -n flux-system get deploy flux-operator                  # Available
kubectl -n flux-system get fluxinstance flux                     # READY True
kubectl -n flux-system get pods                                  # 4 controllers Running

# ⚠ The gate. Without it a missing cluster-topology key becomes "" and
# reconciles GREEN — this is the check with no symptom to notice.
kubectl -n flux-system get deploy kustomize-controller \
  -o jsonpath='{.spec.template.spec.containers[0].args}' | tr ',' '\n' | grep feature-gates
# -> exactly ONE line, containing StrictPostBuildSubstitutions=true

# Source + the three tiers
kubectl -n flux-system get gitrepository flux-system             # READY True
kubectl -n flux-system get kustomizations                        # crds/infrastructure/apps READY True

# Substitution actually resolved — the real test of the whole postBuild chain
kubectl get bgppeer pfsense -o jsonpath='{.spec.peerIP}'         # a real IP, NOT empty
```

**Adoption** is what a first run is really testing — Flux taking over three
things Ansible primed, without a diff war:

```sh
kubectl -n tigera-operator get helmrelease tigera-operator       # READY True
helm -n tigera-operator list                                     # same release, revision bumped by 1 at most
kubectl get nodes                                                # still Ready
kubectl -n calico-system get pods                                # no restart storm
```

A healthy adoption is *boring*: the HelmRelease goes Ready, Calico pods don't
restart, and the BGP session stays up. If the HelmRelease sticks not-Ready,
compare `gitops/infrastructure/calico/values.yaml` against the live release
(`helm -n tigera-operator get values tigera-operator`) — they must be identical,
which is the entire point of the shared values file.

**Idempotency:** re-run `flux-bootstrap.yml` → no changes, and critically the
`GitRepository` and the three Kustomizations are **not** recreated. Phase 1 of
the FluxInstance apply is skipped once the instance exists precisely so a re-run
can't strip `spec.sync` and cascade a prune through everything Flux owns.

## Troubleshooting

**API/`proxmox_kvm` tasks fail with `No route to host` (`EHOSTUNREACH`) on port
8006, yet the SSH-delegated tasks (snippet upload) on the *same* host succeed.**
The `proxmox_*` modules reach the API from the control node's **Python**; SSH
tasks use the `ssh` binary — so this is the control node's Python being unable to
reach the Proxmox LAN, not a config bug. Two known causes, same symptom — tell
them apart with this test (public reachable, local not):

```
uv run python -c "import socket; socket.create_connection(('1.1.1.1',443),5); print('public OK')"
uv run python -c "import socket; socket.create_connection(('<pve-ip>',8006),5); print('local OK')"
```

⚠ **The `<pve-ip>` line must target an ON-LINK host, and that is the whole
trick.** Local Network Privacy gates only **same-link** traffic, so a host
reached *through the gateway* is never gated and is worthless as a control —
using a routed host to "rule out TCC" cannot detect the denial it is meant to
rule out (done for real on 2026-08-29; it cost an hour and produced a confident
wrong diagnosis). Check first:

```
route -n get <ip>     # a `gateway:` line => routed => useless as a control
```

Then confirm with an **Apple-signed binary against the same host:port**, since
those are exempt:

```
nc -z -G 5 <pve-ip> 8006                                        # succeeds
curl -sk -o /dev/null -w '%{http_code}\n' \
     https://<pve-ip>:8006/api2/json/version                    # 401
```

Python raising `Errno 65` while `nc` succeeds and `curl` returns **401**
(reached Proxmox, merely unauthenticated) is TCC, conclusively. If `nc` fails
too, it is *not* TCC — then look at the network.

- **Public OK, local FAILS → macOS Local Network Privacy** (macOS 15+/26). The
  terminal app you launched Ansible from lacks **Local Network** access, so its
  child `python` is blocked from the LAN (Apple's `curl`/`nc` are exempt, which
  is why they mislead you into thinking the network is fine). **Fix:** System
  Settings → Privacy & Security → **Local Network** → enable your terminal app
  (Terminal / iTerm / **Zed** / VS Code / …), then **fully quit and relaunch it**
  (TCC caches the denial for the running process). Re-run the public/local test
  above — the local one should now print `local OK`.
- **Both work in a plain shell but Ansible still fails intermittently → a VPN
  default route.** A Tailscale/WireGuard **exit node** installs `default → utunN`;
  when the LAN host-route/ARP expires, a fresh API connection falls through the
  tunnel, which can't route the RFC1918 Proxmox IP. **Fix:** turn off the exit
  node (Tailscale → Exit Node → None) — a control node directly on the pve subnet
  doesn't need it. Confirm `route -n get <pve-ip>` shows `interface: en0`, not
  `utunN`.
