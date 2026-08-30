# ADR-0009: Secrets: k3s secrets-encryption at rest; External Secrets Operator + Bitwarden Secrets Manager for runtime app secrets

- **Date:** 2026-07 (initial design)
- **Status:** Accepted — the at-rest half is live (secrets-encryption on from boot 1); the ESO half is not yet implemented
- **Supersedes / related:** [ADR-0021](0021-topology-blinding-postbuild-substitution.md) (topology, which is *not* a secret and goes a different route); [ADR-0027](0027-control-node-secrets-bws-runtime.md) (control-node secrets — a third tier, decided later); root `CLAUDE.md` "Secrets, credentials, and topology blinding"; `../architecture.md` §3.6

## Context

Two distinct problems that are easy to conflate: how Kubernetes `Secret`
objects are protected *in the datastore*, and how application secrets *get
into* the cluster from Git without Git ever holding secret material — not even
ciphertext, because ciphertext in history is permanent, unrotatable without a
commit, and unauditable.

## Decision

**Layer 1 — datastore secrets at rest.** k3s `--secrets-encryption` (aescbc),
enabled at first server start, so `Secret` objects are AES-encrypted in the
SQLite datastore, plus full-disk encryption on the control-plane VM. Back the
encryption key up off-cluster. (Verified on from boot 1 on `snoop-a2o`; enabling
it later requires a rotation procedure, so it was never deferred.)

**Layer 2 — GitOps app secrets: External Secrets Operator + Bitwarden Secrets
Manager.** ESO syncs secrets from Bitwarden SM into native `Secret` objects, so
Git holds only `ExternalSecret` *references*. Pods restart offline because the
materialised Secrets persist in the datastore — ESO is needed at *sync* time,
not pod start. Bitwarden SM is zero-knowledge (the provider cannot read the
values), logs every access, and its free tier (unlimited secrets, 3 projects,
3 machine accounts) covers this cluster. The ESO Bitwarden provider runs a
small **Bitwarden SDK Server** in-cluster with a cert from cert-manager.

- *Least privilege:* a dedicated Bitwarden **project** for app secrets, read by
  a **machine-account token scoped read-only to that project** (never the
  personal vault), with an **expiry**. The split of projects by *consumer* —
  control node vs ESO — is in [ADR-0027](0027-control-node-secrets-bws-runtime.md).
- *Supply-chain hygiene:* after the April 2026 `@bitwarden/cli` npm compromise
  (the SDK Server and the `bws` binary are separate from that package), pin
  the SDK Server **image by digest**, pin provider/`bws` versions and verify
  checksums, keep the token scope tight so a compromised tool's blast radius
  is one project, and watch the access audit log.
- *Anti-lock-in:* workloads reference only `ExternalSecret` CRDs, so the
  backing store is swappable — repoint a single `SecretStore` to Vault/OpenBao
  or a cloud manager without touching workloads.

## Alternatives rejected

For layer 1:
- **Cloud KMS as the key-encryption key.** Stronger key separation and audit,
  but adds a cold-start dependency: the control plane needs the KMS reachable
  to decrypt on reboot. Deferred, possible later.

For layer 2:
- **SOPS + a static age key.** One key decrypts everything, and ciphertext in
  Git history means a future key leak retroactively exposes all of it.
- **SOPS + cloud KMS, ciphertext in Git.** A revocable key fixes the blast
  radius, but ciphertext still accumulates in Git, and it is more parts.
- **SOPS + Flux Bucket/OCI source.** Meets zero-trust and not-in-Git, but
  needs an encrypt-push pipeline and a key to manage.
- **SOPS ciphertext stored in a cloud secret manager.** Does not work natively
  — ESO copies values verbatim and never decrypts SOPS.
- **A cloud secret manager directly (GCP SM / AWS SSM) via ESO.** The provider
  sees plaintext, and it adds vendor sprawl.
- **Self-hosted Vault / OpenBao.** Operational weight (seal/unseal, HA) and a
  security service to keep patched.
- **Self-hosted Infisical in-cluster.** Circular bootstrap dependency.
- **Vault Agent injector.** Needs Vault live at pod start; ESO instead
  materialises persistent Secrets so workloads restart offline.
- Bitwarden SM chosen because: zero-knowledge, already in use (no new vendor),
  free tier covers it, scoped + expiring machine token, and ESO keeps the
  backing store swappable. The SOPS+OCI alternative was dropped specifically
  because Bitwarden's zero-knowledge model already gives the "cloud can't read
  plaintext" property with fewer moving parts — no SOPS key, no OCI pipeline.
  Both paths end with plaintext Secrets in-cluster anyway, protected by layer 1.

## Consequences

- **Never commit a credential in any form, including ciphertext.** BWS gives
  rotation, revocation, and audit; use it.
- **ESO cannot be pulled earlier in the chain.** The Bitwarden SDK Server
  needs a cert-manager cert, which needs a Gateway, which needs a LoadBalancer
  IP. That cycle is real, so anything needed *before* ESO exists is an
  **Ansible-seeded `Secret`** at bootstrap. Don't try to solve it by moving
  ESO up. Today that covers the `cluster-topology` Secret
  ([ADR-0021](0021-topology-blinding-postbuild-substitution.md)) and — if one
  is ever used — a BGP peer password.
- Topology values (peer IP, LB range) are *not* secrets and do **not** go
  through ESO; they are blinded with `${var}` substitution
  ([ADR-0021](0021-topology-blinding-postbuild-substitution.md)). SOPS/age is
  the fallback only where substitution cannot go (whole blocks/lists, or values
  needed at kustomize-*build* time); the age key would then be just another
  BWS secret, Ansible-seeded as `sops-age`.
- ESO's own access token lives in the **control-node** project, not the apps
  project — the thing that grants access cannot live behind the access it
  grants ([ADR-0027](0027-control-node-secrets-bws-runtime.md)).
- Most secrets regenerate (k3s tokens, machine tokens, the aescbc key, TLS
  certs); the truly irreplaceable ones are covered by
  [ADR-0015](0015-backups-nas-s3-and-break-glass.md).
