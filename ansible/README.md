# ansible/ — Flatcar VM provisioning

Current scope: create a correctly-shaped, reachable, **rebuildable** Flatcar VM
shell — two NICs, a separate data disk, key-only SSH — via Ignition delivered
through Proxmox's config-drive (`cicustom`). **No k3s yet** (see
`ansible/CLAUDE.md` §1 for the definition of done).

## Layout

```
ansible.cfg              # inventory + roles paths, vault settings
requirements.yml         # community.proxmox collection (+ butane on PATH)
inventory/
  hosts.yml              # localhost + the PVE host (SSH, for snippet/qm)
  nodes.yml              # node map — SOURCE OF TRUTH for node identity/addressing
  group_vars/            # adjacent to the inventory so it loads for every playbook
    all/                 # a DIRECTORY -> every file loads for group `all`
      vars.yml           # structure + {{ vault_* }} refs (nothing sensitive)
      vault.yml          # (git-ignored) real encrypted values — you create this
    vault.example.yml    # template; sits OUTSIDE all/ so it never auto-loads
roles/
  flatcar_template/      # download proxmoxve image -> import -> template (idempotent)
  flatcar_vm/            # render Butane -> Ignition, clone, pin MACs, disk, cicustom, boot
playbooks/
  build-template.yml     # build the Flatcar template
  provision-nodes.yml    # provision every node in nodes.yml
site.yml                 # both, in order
```

## One-time setup

The control-node Python toolchain (ansible-core, proxmoxer, requests) is managed
with [uv] — it owns the interpreter, the venv, and the lockfile. Run these from
the **repo root** (where `pyproject.toml`/`uv.lock` live); the rest from `ansible/`.

1. `uv sync` — creates `.venv` with the exact pinned deps from `uv.lock`
   (installs Python 3.12 automatically if you don't have it).
2. `uv run ansible-galaxy collection install -r requirements.yml` — installs
   `community.proxmox` into the in-repo `.ansible/` path (isolated, like the venv).
3. Install `butane` on the control node — it's a **Go binary, not pip/uv**: grab
   the upstream release binary (or `brew install butane` once Homebrew is set up).
4. `cp inventory/group_vars/vault.example.yml inventory/group_vars/all/vault.yml`
   and fill in the real values — the Proxmox API credential (create it per
   **[Proxmox API token & user](#proxmox-api-token--user-one-time-on-a-pve-node)**
   below) **and** the environment specifics (IPs, subnets/VLANs, gateways,
   hostnames, storage names, SSH public key). Then `uv run ansible-vault encrypt
   inventory/group_vars/all/vault.yml`. (It goes *inside* `all/` so it loads;
   the `.example` stays outside so it doesn't.)
5. `inventory/group_vars/all/vars.yml` needs no editing for secrets — it's just
   structure plus `{{ vault_* }}` references. Only generic, non-revealing
   defaults (MAC OUI, Flatcar channel/version) remain in cleartext there.
6. `inventory/hosts.yml` needs no editing — the PVE host's address and login
   user are `{{ vault_* }}` references too (fill them in the vault above).

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

Step 4 prints the token **value** exactly once — copy it into
`vault_proxmox_api_token_secret`. It can't be retrieved later; if lost, delete
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
- This token is only the **API** half. The snippet upload and
  `qm set --cicustom` go over **SSH** — see below.

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

# 3. Passwordless sudo, SCOPED to qm only (not ALL) — this is the whole point
echo 'provisioner ALL=(root) NOPASSWD: /usr/sbin/qm' >/etc/sudoers.d/provisioner
chmod 440 /etc/sudoers.d/provisioner
visudo -cf /etc/sudoers.d/provisioner    # syntax-check

# 4. Let provisioner write the Ignition snippet WITHOUT sudo (keeps the sudoers
#    to just `qm`). Proxmox references snippets by a FLAT volid
#    (`<storage>:snippets/<file>` — no subdirectories), so the file must land
#    directly in the snippets dir (= vault_proxmox_snippet_dir). Keep that dir
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

Then set `vault_proxmox_ssh_user: "provisioner"` in your vault (the default in
`vault.example.yml`). The roles invoke `qm` via the `proxmox_qm` helper
(`inventory/group_vars/all/vars.yml`), which resolves to `sudo qm` for a
non-root user and plain `qm` for root — so it works either way.

Verify it end-to-end (needs `--ask-vault-pass` if the vault is encrypted):

```bash
ssh provisioner@<pve-host> 'sudo qm list'   # scoped sudo works, no password
uv run ansible proxmox -m raw -a 'sudo qm list' --ask-vault-pass
```

With this in place you can set `PermitRootLogin no` in the PVE host's `sshd`
and never SSH as root for provisioning.

- **Why scoped and not `NOPASSWD: ALL`?** `qm` is the only root-only command
  the roles run; the snippet write is handled by owning the dir (step 4). If
  you later add a role step that shells out to another root-only tool (e.g.
  `pvesm`), extend the sudoers line accordingly.

## Run

Prefix ansible commands with `uv run` so they use the pinned venv (or activate it
once with `source .venv/bin/activate` and drop the prefix):

```bash
uv run ansible-playbook site.yml --ask-vault-pass              # template + all nodes
uv run ansible-playbook playbooks/provision-nodes.yml \
    -e node_filter=snoop-a2o --ask-vault-pass                  # just one node
```

[uv]: https://docs.astral.sh/uv/

## Adding a node

Add one entry to `inventory/nodes.yml` with a unique `node_number` (1..254);
the DMZ IP (`<dmz_subnet_base>.<n>`), Ceph-public IP (`<ceph_subnet_base>.<n>`),
MACs, and `vmid` (1000+n) are all derived from it (subnet bases come from the
vault). A preflight assert enforces `node_number` uniqueness.

## Verify (definition of done)

After boot, over SSH to the node's DMZ IP (`<dmz_subnet_base>.<n>`):

- `ip a` — static addresses on both NICs; `eth1` MTU **8996**, not 1500
- `ip link show` — NIC MACs match the derived `<mac_oui>:00:<n hex>:0{0,1}`
- `ip route show dev eth1` — only the connected subnet, **no default route**
- `resolvectl status` / `getent hosts <name>` — DNS via the vaulted resolver
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
  the `k3s_version` pin
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
