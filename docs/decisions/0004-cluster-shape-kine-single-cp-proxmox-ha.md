# ADR-0004: Cluster shape: SQLite via kine, one tainted control plane with HA from Proxmox, 1 CP + 3 workers

- **Date:** 2026-07 (initial design)
- **Status:** Accepted (only the single all-in-one node exists so far — see `../worklog.md`)
- **Supersedes / related:** [ADR-0003](0003-k3s.md); [ADR-0006](0006-ceph-csi-external-proxmox-ceph.md) (the storage that makes the datastore durable); `../architecture.md` §3.1–§3.2

## Context

Three separate questions have one shared answer, so they are recorded
together: what the Kubernetes datastore is, where control-plane availability
comes from, and how many nodes of which kind.

The cluster runs as VMs on a Proxmox HA cluster with replicated Ceph. That
layer already provides "restart the VM on a surviving host when a host dies."
Duplicating that inside Kubernetes with an etcd quorum would cost a steady
~1 vCPU per member and three control-plane VMs, for a single-operator lab.

## Decision

- **Datastore: embedded SQLite via kine. No etcd.** A single server; the
  SQLite file lives on replicated Ceph and so survives host failure.
  Optionally add **Litestream** for continuous off-site point-in-time backup of
  the datastore, independent of storage replication.
- **Control-plane HA = Proxmox HA, not Kubernetes multi-master.** If the host
  dies, Proxmox restarts the control-plane VM elsewhere; workloads on the
  agents keep running through the brief API blip.
- **Upgrades:** snapshot the control-plane VM → upgrade k3s → verify (or roll
  the snapshot back). A single-server k3s upgrade is a sub-minute
  *control-plane* blip, not cluster downtime.
- **Node layout: 1 dedicated control-plane VM + 3 workers.** The control-plane
  VM is small (2 vCPU / 4 GB) and **tainted**
  (`node-role.kubernetes.io/control-plane=true:NoSchedule`) so no workload
  lands on it. Workers are sized for the workload set (RAM-heavy for
  Qdrant/Postgres).

## Alternatives rejected

- **Embedded etcd HA (3 servers).** Exactly the footprint being avoided; only
  needed for multi-master, which Proxmox HA makes unnecessary.
- **External Postgres/MySQL datastore.** Gives etcd-free HA, but the database
  must then live *outside* the cluster and be made reliable itself — an extra
  moving part.
- **dqlite / rqlite.** Raft underneath, i.e. the same consensus cost as etcd;
  kine does not support them and dqlite is deprecated in k3s. Multi-master
  SQLite replication also violates kine's strictly-+1 revision requirement (it
  needs a single writer).
- **Managed cloud control plane (EKS Hybrid Nodes and similar).** No free
  tier, and it parks etcd — hence every Secret — in the cloud, against the
  on-prem goal.
- **"Temporarily add a control-plane node for upgrades."** Impossible from
  SQLite: multiple servers require etcd, and SQLite→etcd is a one-way
  migration.
- **3 nodes (tainted server + 2 agents).** Fewer VMs but less spread and less
  isolation than 1 + 3.

## Consequences

- The control plane is a single point of scheduling failure for API
  availability, mitigated only by Proxmox HA restart. Accepted; the real blip
  duration during a Proxmox HA restart is still worth measuring rather than
  assuming (open test, see `../worklog.md` / ansible `CLAUDE.md` open items).
- The all-in-one node that exists today deliberately carries **no** CP taint;
  the taint is added when workers arrive.
- The datastore's durability rides on the Ceph decision
  ([ADR-0006](0006-ceph-csi-external-proxmox-ceph.md)) and on the data disk
  being mounted at k3s's default `/var/lib/rancher` — see ansible `CLAUDE.md`
  for why a `data-dir` override was tried and rejected.
- Because the cluster is currently disposable, version bumps are
  re-provisions, not in-place upgrades; the snapshot-upgrade procedure above
  becomes the norm only once there is state worth keeping.
