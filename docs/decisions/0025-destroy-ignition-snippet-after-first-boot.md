# ADR-0025: `provision-nodes.yml` waits for SSH, then detaches `cicustom` and deletes the `.ign` — it embeds the k3s join token

- **Date:** 2026-08-03 (raised, decided, implemented and verified the same day)
- **Status:** Accepted
- **Supersedes / related:** [ADR-0005](0005-flatcar-k3s-sysext-ignition-config-drive.md) (how the snippet gets there), [ADR-0027](0027-control-node-secrets-bws-runtime.md) (where the token's durable copy lives), [ADR-0026](0026-per-cluster-derivation-from-index.md) (per-cluster tokens — the blast radius this bounds). Code: `ansible/playbooks/provision-nodes.yml`, `ansible/playbooks/tasks/destroy-ignition-snippet.yml`, `ansible/roles/flatcar_vm/tasks/main.yml`. Operator notes: `ansible/README.md` → "The Ignition snippet is destroyed after first boot".

## Context

The per-node `.ign` on the shared snippet storage embeds `token:`
(`k3s-config.yaml.j2`) — the k3s cluster join token, which admits any node
holding it to the cluster and from which k3s derives the datastore
bootstrap-data encryption key. **Ignition reads it exactly once**: the
`ignition.firstboot` flag is cleared afterwards, so past first boot the file is
a live credential with no remaining purpose, sitting on CephFS reachable from
all three hypervisors. The token's other two copies — BWS, and
`/etc/rancher/k3s/config.yaml` on the node — are both load-bearing; this one
isn't, and it has the widest blast radius.

A related hardening had already landed on 2026-08-02: the upload was
`mode: "0644"`, i.e. world-readable, protected only by the snippet dir's `2770`
— which PVE had just been observed resetting to `0755`. It is now `0660`, so
exposure takes two independent regressions instead of one.

## Decision

**`provision-nodes.yml` does not stop at "VM is running". It waits for SSH on
port 22 on every node it provisioned, and then, for each node that answered,
removes `cicustom` from the VM config (over the API) and deletes
`<hostname>.ign` from the snippet storage (over SSH).**

- **Why `provision-nodes.yml`, not `bootstrap-cluster.yml`** (agreed
  2026-08-03): it keeps a Proxmox concern in the Proxmox playbook, that play
  already owns the VM lifecycle (so it owns `cicustom`), and it stays correct
  for a node that is provisioned but never bootstrapped.
- **SSH on :22 is a genuine Ignition-completed signal**, not a proxy for one:
  the admin user and its `authorized_keys` come *from* Ignition, so sshd
  answering means the config was consumed. `bootstrap-cluster.yml` already
  used exactly this check; it is reused.
- **⚠ ORDER IS LOAD-BEARING: `cicustom` comes off the VM config FIRST, THEN
  the file is deleted.** PVE regenerates the cloud-init drive on *every* VM
  start, and `read_cloudinit_snippets_file` ends in
  `PVE::Tools::file_get_contents($full_path, ...)` with **no error handling**
  (verified in `/usr/share/perl5/PVE/QemuServer/Cloudinit.pm`, PVE 9.2.3).
  Delete the file while `cicustom` still points at it and `qm start` dies —
  not at the next provision, but at **the node's next reboot**, the worst time
  to find out.
- **Detach goes over the API** (`proxmox_kvm` `delete: cicustom`) — the role
  already *attaches* it that way, so the pair is symmetric and the scoped sudo
  stays untouched (prefer the API; keep SSH to the snippet file itself). **The
  file delete stays on SSH** like the upload, and needs no sudo: unlinking
  needs write on the setgid dir, the same grant the upload uses.
- **Safe on a RUNNING VM — verified two ways in the PVE source**, because "it
  lands in `[PENDING]`" would have made the ordering argument worthless:
  (1) every key of `$confdesc_cloudinit` — `cicustom` included — is in
  `$fast_plug_option` (`QemuServer.pm`), so `vmconfig_hotplug_pending` applies
  the delete to the **live config immediately**; (2) even if it were deferred,
  `vm_start_nolock` applies pending changes and reloads the config before
  `apply_cloudinit_config`, so the drive can never be regenerated against a
  stale `cicustom`. Confirmed empirically: after the detach, `qm config
  --current 0` shows nothing pending.
- **`ide2` STAYS.** A cloud-init drive is standard on any PVE cloud-init VM,
  the template ships `--ide2 <storage>:cloudinit` so every clone gets one back,
  and `cicustom` needs it to land on at the next rebuild — removing it would be
  a hardware change re-done on every rebuild, forever. What PVE generates for
  it with `cicustom` gone carries nothing sensitive (`qm cloudinit dump <vmid>
  user`: `hostname`/`fqdn`/`users: default`/`package_upgrade` — no `sshkeys`,
  no `cipassword`, because none are ever set on these VMs). The credential was
  in the *snippet*, never in the drive.

## Alternatives rejected

- **Clean up in `bootstrap-cluster.yml`** — mixes a Proxmox concern into the
  cluster play and leaves a provisioned-but-never-bootstrapped node holding a
  live token on shared storage.
- **Delete the snippet, leave `cicustom`** — breaks the next reboot (above).
- **`qm set --delete cicustom` over SSH** — works, but the API path is
  symmetric with the attach and keeps the scoped sudoers untouched.
- **Also remove `ide2`** — a hardware change redone every rebuild, for no
  security gain.
- **Keep the snippet as a debugging record** — the on-storage record of "what
  config did this node get" is reproducible from BWS + the node map, so it's a
  convenience, not data; `destroy_ignition_snippet: false` buys it back when
  debugging a first boot.

## Consequences

- **⚠ Restore trap.** A VM backup taken *before* the cleanup restores a config
  that still references a snippet which no longer exists — **the VM won't
  start**. Recovery is re-running `provision-nodes.yml` for that node (it
  re-uploads the snippet and re-sets `cicustom`), or `qm set <vmid> --delete
  cicustom` by hand. Worth knowing during a restore rather than deriving at
  2am.
- `destroy_ignition_snippet: false` skips the cleanup **and** the SSH
  up-check, so the play won't block on a node that never boots — the escape
  hatch while debugging a first boot. `node_boot_timeout` (default 300 s)
  bounds the wait.
- **One node failing to boot no longer strands the other nodes' tokens:**
  cleanup runs for everything that came up, then the play fails naming what
  didn't, leaving those `.ign`s deliberately in place (deleting them with
  `cicustom` still attached is the trap above). Fix the boot, re-run, and the
  cleanup finishes.
- The play now asserts `node_filter` matched something — a typo used to
  provision nothing, silently, which is worse now that the same filter drives
  cleanup.
- **Ansible gotcha baked in:** `flatcar_vm` sets `hostname`/`vmid`/`eth0_ip`
  as *facts*, and **facts outrank task vars** — so a cleanup task reusing those
  names would silently get the LAST provisioned node's values and detach
  `cicustom` from someone else's VM. Every var in
  `destroy-ignition-snippet.yml` is `cleanup_*`-prefixed for that reason, and
  a `proxmox_vm_info` read + assert (*exactly one VM, and its name == this
  node*) is the backstop. Secondarily, `proxmox_kvm` hard-codes `changed=True`
  on `delete`, so the task gates on `config.cicustom` to stay honest.
- **Related hardening, same class — the snippet dir self-repairs.** PVE
  recreates storage content subdirectories as `root:root 0755` on storage
  activation (observed 2026-08-02 and 2026-08-03; a template rebuild or even
  deleting a file is enough), silently undoing the `chgrp`/`chmod` from the
  README's setup while group membership stays intact. `flatcar_vm` stats the
  dir, repairs group+setgid via two fixed-argument sudoers rules installed on
  **every** PVE node, re-stats, and asserts. The repair-path fix initially
  broke the happy path (a skipped task still registers, wiping the good stat);
  both paths are now verified — *a fix verified only on the failure it was
  written for is half-verified.* Details in `ansible/README.md` → "Proxmox SSH
  access".

## Evidence

Verified end-to-end on `snoop-a2o` 2026-08-03 **including a full `qm reboot`**
(a stop→start, so PVE really regenerates the drive — a guest-side `reboot`
would not test this): `qm reboot` exits 0, SSH answers in ~10 s, node
**Ready**, all pods Running/Completed. Journal shows `ignition-subsequent.target
— Subsequent (Not Ignition) boot complete` and `ignition-delete-config.service
... skipped (ConditionFirstBoot=true)` — Ignition provably did **not** re-run.
`sr0` shows up labelled `cidata` but mounted nowhere. Afterburn was the one
real candidate for reading the regenerated drive and it's clean:
`coreos-metadata-sshkeys@core.service` runs every boot but its only source is
`authorized_keys.d/ignition`; the `admin` user we SSH with is untouched. See
[`../worklog.md`](../worklog.md).
