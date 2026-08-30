# ADR-0005: Node OS: Flatcar with the k3s sysext; Ignition delivered via the cloud-init config drive

- **Date:** 2026-07 (initial design); delivery validated live 2026-07-07
- **Status:** Accepted
- **Supersedes / related:** [ADR-0003](0003-k3s.md); [ADR-0007](0007-ansible-not-terraform.md) (who generates and delivers the Ignition); [ADR-0017](0017-static-addressing-no-dhcp.md) (reverses the DHCP assumption below); [ADR-0025](0025-destroy-ignition-snippet-after-first-boot.md); [ADR-0030](0030-flatcar-os-update-policy.md); `../architecture.md` §3.1, §3.3, §3.4

## Context

The nodes are VMs that should be immutable and rebuildable from a description
in Git, with no manual post-boot steps. That needs an OS that is configured
once at first boot from a declarative file, and a way to get that file into
the VM on Proxmox using only a scoped API token (no `root@pam`).

## Decision

- **Flatcar Container Linux**, provisioned by **Ignition** (no PXE, no
  Ignition server).
- **k3s is installed via the k3s sysext** from `flatcar/sysext-bakery`:
  immutable, updated by `systemd-sysupdate`, Renovate-trackable.
- **Ignition is delivered through the cloud-init config drive.** Each node's
  Ignition JSON is set as the cloud-init *user-data* with
  `--cicustom "user=<storage>:snippets/<node>.ign"`; the `proxmoxve` image's
  default OEM reads Ignition from there. Because Flatcar cannot consume
  Ignition *and* cloud-config through the same user-data slot, the Proxmox
  cloud-init GUI fields go inert and **all** node identity, keys, and
  networking live in the Ignition.
- Node reboots for OS updates should be coordinated with the **Flatcar Update
  Operator (FLUO)** so they cordon/drain first. (Whether/when to do that is
  [ADR-0030](0030-flatcar-os-update-policy.md).)

**Status note:** the original entry said to validate delivery on a single
hand-built node before generalising into the Ansible loop. That validation
happened on `snoop-a2o` on 2026-07-07 — the proxmoxve OEM consumes raw
Ignition JSON from the config drive cleanly, and a from-scratch rebuild
reproduces the node identically. See `../worklog.md`.

## Alternatives rejected

- **PXE / an Ignition HTTP server.** A bare-metal / fleet pattern; unneeded for
  VMs on a hypervisor that can hand the guest a file directly.
- **k3s binary dropped in `/opt/bin`.** Loses the immutable, sysupdate-managed,
  Renovate-trackable lifecycle the sysext gives for free.
- **Ignition via fw_cfg (`args: -fw_cfg ... file=`).** Requires setting QEMU
  `-args`, which Proxmox **locks to `root@pam`** — at odds with scoped-token
  automation. The oft-cited "comma-escaping pain" is specific to the inline
  `string=` variant; `file=` avoids it, but the root@pam requirement remains.
  Community bpg+Flatcar modules historically used fw_cfg mainly because they
  **predate the proxmoxve image** (the generic qemu OEM reads fw_cfg). This
  decision supersedes two earlier swings — "cloud-init drive is the clean path"
  and later "community modules lean on fw_cfg" — and lands on the config drive
  for an explicit root@pam reason.
- **Relying on cloud-init's `network-data` for addressing.** The original
  design said "DHCP sidesteps the fragile config-drive network-data path."
  That is half right: the network-data path *is* fragile and is avoided — but
  by defining static addresses in Ignition's own `systemd-networkd` units, not
  by DHCP. See [ADR-0017](0017-static-addressing-no-dhcp.md).

## Consequences

- The config-drive route needs **no `root@pam`** — only host file access for
  the snippet upload, which is why the PVE SSH user has sudo scoped to `qm`
  and two fixed repair commands (see `ansible/README.md`).
- The `.ign` snippet embeds the k3s join token and sits on shared storage
  every hypervisor can reach; it is read exactly once. Hence
  [ADR-0025](0025-destroy-ignition-snippet-after-first-boot.md).
- Ansible only *generates and delivers* the Ignition; it never converges a
  running node. There is no Python on Flatcar, so anything Ansible must do on
  a node goes through `raw` (see ansible `CLAUDE.md`).
- The Ignition `files` stage runs in the initramfs, which has **no network**
  here (no DHCP; static config activates only after the pivot). A remote
  `contents.source:` therefore boot-loops the node. The sysext is fetched by a
  post-pivot systemd unit instead. Repo rule: never put a remote
  `contents.source:` in Ignition.
- k3s's `k3s.service` lives *inside* the sysext and is absent at Ignition
  time, so it is enabled with a `storage.links` wants/ symlink, not
  `systemd.units[].enabled`.
- The version pinned in `group_vars` is only the **seed** for a fresh node;
  sysupdate moves it within the pinned minor afterwards, so a provisioned-vs-
  running delta is expected, not drift.
