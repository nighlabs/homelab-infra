# ADR-0006: Persistent storage: ceph-csi-operator against the existing Proxmox Ceph

- **Date:** 2026-07 (initial design)
- **Status:** Accepted — not yet implemented (ceph-csi lands after Calico BGP → Gateway → cert-manager in the delivery order)
- **Supersedes / related:** [ADR-0004](0004-cluster-shape-kine-single-cp-proxmox-ha.md) (datastore durability rides on this); [ADR-0017](0017-static-addressing-no-dhcp.md) (the second NIC on the Ceph public VLAN); `../architecture.md` §3.5; root `CLAUDE.md` network topology

## Context

The Proxmox cluster already operates a Ceph cluster, and that cluster
**already serves live Proxmox VM storage today**. The Kubernetes tier needs
RWO block storage for Postgres/Qdrant/Redis and RWX file storage for anything
shared, and it needs volumes that are not bound to a node so a dead worker
does not strand its data.

## Decision

- **Reuse the Proxmox Ceph. Do not run a second Ceph inside Kubernetes.**
- Deploy **ceph-csi via the ceph-csi-operator** (the only supported ceph-csi
  deployment mode as of v3.16), pointed at the external cluster.
- Two StorageClasses: **`ceph-rbd`** (RWO block) for Postgres/Qdrant/Redis;
  **`cephfs`** (RWX) for anything needing shared file access.
- Enable **Non-Graceful Node Shutdown** (the `out-of-service` taint) so RBD
  detaches from a hard-failed node and the rescheduled pod can re-attach on a
  healthy one.

**Setup checklist (still to do):**
- a dedicated Ceph **pool + restricted client user** for Kubernetes — never
  touch PVE's VM pool;
- a **second vNIC on the Ceph public VLAN** per node so the CSI client reaches
  the mons/OSDs directly (already provisioned — see
  [ADR-0017](0017-static-addressing-no-dhcp.md));
- **match the ceph-csi version** to the Proxmox Ceph release;
- watch RBD/CephFS image features (exclusive-lock, object-map, …) against the
  Flatcar kernel — provision one test RBD PVC and one CephFS mount before
  trusting either class.

**Upgrade order:** confirm version overlap → upgrade the Proxmox Ceph cluster
(mons → mgr → OSDs, verify, finalise with `require-osd-release`) → upgrade
ceph-csi via the operator → test a PVC. Clients tolerate skew within the
supported window.

## Alternatives rejected

- **In-cluster Rook-managed Ceph.** Don't run a second Ceph when Proxmox
  already operates one — double the OSD footprint on the same disks for no
  benefit.
- **Rook-external.** Also works, but now sits on the same ceph-csi-operator
  anyway; heavier than ceph-csi direct when Proxmox owns Ceph's lifecycle.
- **local-path.** Node-bound PVs; failover waits for the VM to be restarted
  rather than the pod being rescheduled.
- **Longhorn / OpenEBS.** Worse on virtual disks, and they need the NVMe-TCP
  kernel module that Flatcar lacks.

## Consequences

- **Any change to that Ceph cluster (mons, networks, pools) affects existing
  production workloads, not just this project.** Treat it with the caution
  that implies.
- The k3s nodes touch **only the Ceph public network**, on a tagged VLAN on
  the jumbo bond at MTU 8996. The Ceph *cluster* (replication) network is
  untagged on the same bond, so leaving the secondary NIC untagged silently
  lands it on replication traffic — the VLAN tag is what keeps clients on the
  right network. Jumbo frames must be set end-to-end (VM NIC definition *and*
  the guest networkd unit), or the link silently caps at 1500.
- Only the mons' public addresses are configuration; they are environment
  topology and stay out of Git (root `CLAUDE.md`).
- The Postgres backup path deliberately pushes *out* of Ceph
  ([ADR-0015](0015-backups-nas-s3-and-break-glass.md)) so a Ceph problem is
  not also a backup problem.
