# ADR-0030: Flatcar auto-update/reboot policy is unset — the default is in force

- **Date:** 2026-08-02 (raised)
- **Status:** **Open** — decide before there is Ceph-backed state
- **Supersedes / related:** [ADR-0005](0005-flatcar-k3s-sysext-ignition-config-drive.md) (the design said "coordinate auto-updates with FLUO"; nothing implements it yet), [ADR-0024](0024-calico-ebpf-dataplane-no-kube-proxy.md) (eBPF results are kernel-dependent), [ADR-0004](0004-cluster-shape-kine-single-cp-proxmox-ha.md) (a single control plane makes unplanned reboots visible). Code: `ansible/roles/flatcar_vm/templates/butane.yaml.j2`, `ansible/roles/flatcar_template/tasks/main.yml`.

## Context

There is **no `update-engine`/`locksmith` configuration anywhere** in the
Ignition templates (grepped `roles/` + `playbooks/`), so Flatcar's default
auto-update-and-reboot is in force. The VM shell's definition of done
deliberately proved unattended reboot works — that same mechanism now means
**nodes move to new stable on their own schedule**.

## Current state (no decision yet)

The default stays, knowingly, while the cluster is disposable.

- **Consequence for testing:** you cannot pin the tested OS by controlling the
  template; the node walks away from it. That matters most for eBPF — its
  behaviour is kernel-dependent, so an unattended reboot into a new kernel
  mid-trial reads as a flake, and a "verified" result may be against a kernel
  you're no longer running. **Record OS + kernel with every trial result**
  (`4593.2.4` / `6.12.95-flatcar` as of 2026-08-02).
- **Related trap — the template is a pin by accident.** `flatcar_version:
  "current"` means a *rebuild* always fetches latest stable, but
  `flatcar_template`'s build is guarded on `qm status <vmid>` failing, so
  `build-template.yml` **runs green and silently skips** whenever vmid 9000
  exists. A successful run is not evidence of a fresh template. Rebuilding =
  `qm destroy 9000` then re-run (safe: clones are `full: true`, so existing
  VMs are independent). A stale template also means every provision boots
  old, then auto-updates and reboots shortly after — the mid-trial reboot,
  near-scheduled.

## Options

- **Leave the default** — fine while disposable, and why the DoD tested it.
  It stops being fine once there's Ceph-backed state on a single control-plane
  node: an unplanned CP reboot is an API blip plus whatever the workloads
  notice.
- **k8s-aware drain-then-reboot** — the Flatcar Update Operator (FLUO), as the
  original design intended, so reboots cordon/drain first. Costs a controller
  and needs ≥2 nodes to be meaningful.
- **`reboot-strategy: off`** — **don't.** That trades unplanned reboots for
  unpatched nodes.

## Cheap mitigation available now

Add a `flatcar_template_force` flag so a template rebuild is `-e
flatcar_template_force=true` rather than a manual `qm destroy`, and so the
silent skip stops being a trap.

## Evidence

Nothing decided; nothing to verify. The silent-skip behaviour and the missing
update config were both observed on 2026-08-02.
