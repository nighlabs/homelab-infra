# ADR-0015: Backups: NAS as S3 target (Velero, CNPG Barman, Qdrant snapshots); crown-jewels / break-glass

- **Date:** 2026-07 (initial design)
- **Status:** Accepted — not yet implemented
- **Supersedes / related:** [ADR-0006](0006-ceph-csi-external-proxmox-ceph.md) (backups deliberately leave Ceph); [ADR-0009](0009-secrets-aescbc-and-eso-bitwarden.md); [ADR-0027](0027-control-node-secrets-bws-runtime.md) (the BWS export is the offline copy); `../architecture.md` §8

## Context

Full rebuildability is: Ansible rebuilds the VMs, Flux repopulates workloads
from Git, and Ceph PVs + backups hold the data. The last leg needs a backup
target that is *not* the same Ceph cluster the data lives on, and a short list
of things that cannot be regenerated at all.

## Decision

**Target: the existing NAS, via S3.** Run MinIO (or the NAS's native S3) as
the backup destination. The NAS's existing backup job covers retention and
off-site.

- **Cluster-wide:** Velero (CSI snapshots + Kopia) → NAS-S3, for PVCs and
  Kubernetes resources.
- **Postgres:** CloudNativePG native backup (Barman → S3) → NAS-S3. This
  pushes the data *out* of Ceph with point-in-time (WAL) recovery.
- **Qdrant:** snapshot API → NAS.
- **Schedule a periodic restore drill.** Untested backups are not backups.

**Crown jewels / break-glass.** Because ESO makes Kubernetes Secrets
projections of Bitwarden, most secrets regenerate (k3s tokens, the GHCR
credential, the Bitwarden machine tokens, the aescbc key, TLS certs). The
truly irreplaceable set gets an **offline** backup:

1. **Bitwarden account recovery** (recovery code + 2FA) plus a periodic
   encrypted **export** of the Secrets Manager secrets;
2. an **off-site copy of the Git repo** (which now includes the Ansible
   repo);
3. the **data backups** above.

## Alternatives rejected

- **CNPG → CephFS.** Leaves the backup stuck inside Ceph — a Ceph problem is
  then also a backup problem.
- **Ceph-only durability (replication as backup).** Replication is not
  point-in-time recovery and does not survive a Ceph-level fault.

## Consequences

- The original list included "the Ansible repo + Vault key" as a crown jewel.
  That is **stale**: there is no Ansible Vault key any more
  ([ADR-0027](0027-control-node-secrets-bws-runtime.md)). The BWS access token
  is in the macOS Keychain and is itself regenerable; the periodic encrypted
  BWS export is the offline copy that covers a Bitwarden-specific outage.
- Backups need the NAS reachable from the cluster network — a firewall rule to
  add alongside the LoadBalancer-range rules.
- The restore drill is the acceptance test for this ADR, not the first
  successful backup.
