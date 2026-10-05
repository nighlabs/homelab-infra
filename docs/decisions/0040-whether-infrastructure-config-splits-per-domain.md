# ADR-0040: Whether `infrastructure-config` splits into per-domain Kustomizations

- **Date:** 2026-10-04 (raised at the ESO milestone, which was where it was due to be decided)
- **Status:** **Open.** ESO ships in the existing tier. Decide after observing a real ESO refresh failure, not before.
- **Supersedes / related:** takes over the "Does `infrastructure-config` split into per-domain Kustomizations?" entry from `README.md`'s open questions, where the full history of the question is preserved. [ADR-0038](0038-three-vaults-platform-secrets-seed-and-sync.md) decided the coupled adoption question and added the data point below. Related: [ADR-0020](0020-crd-tier-vendored-server-side-apply.md) (the kind-based tiers), [ADR-0034](0034-secrets-store-1password.md) (which removed the only hard forcing edge). Code if acted on: `gitops/deployment/<cluster>/infrastructure-config.yaml`, `apps.yaml`.

## Context

`infrastructure-config` holds the CRs consumed by controllers one tier up:
cert-manager's issuers and wildcard `Certificate`, ceph-csi's CRs and
StorageClasses, and now ESO's two ClusterSecretStores and three platform
ExternalSecrets. kustomize-controller applies a tier in one pass, and
`wait: true` makes the tier's Ready the conjunction of everything kstatus can
see in it.

**There is no correctness argument for splitting.** The one hard forcing edge
(the Bitwarden SDK Server's need for a cert-manager `Certificate`) disappeared
with ADR-0034. ADR-0038 then chose *not* to source anything a rebuild needs
from ESO. The Ansible seed stays authoritative for existence, so nothing in
this tier waits on another member.

**What ADR-0038 adds is a sharper soft cost.** The platform ExternalSecrets
and ClusterSecretStores report real Ready conditions. Under `wait: true`, a
1Password outage, an expired token or a rate-limit lockout (a 24-hour one on
Individual/Families, ADR-0034) turns the whole tier NotReady. That stops
`apps` from reconciling, even though cert-manager and ceph-csi keep working
from their seeded Secrets. Running pods are unaffected; changes to `apps` are
not applied. The same is true today of a cert renewal failure (the
`Certificate` goes NotReady while storage is fine).

## Options

1. **Status quo.** One tier, one Ready. Simple and kind-based. Costs: a
   NotReady that does not say whether the problem is PKI, storage or secrets;
   one `timeout: 10m` sized for ACME and inherited by everything; and any one
   domain's degradation gates all of `apps`.
2. **Per-domain split.** `pki`, `storage` and `secrets` Kustomizations, each
   `dependsOn: infrastructure`, with `apps` depending on all three. Ready per
   domain and a timeout per domain. Still gates `apps` on all three, which is
   *correct*: apps need certs, storage and secrets. Cost: a second organizing
   principle (domain) layered on the kind-based tiers, which must then be
   adopted consistently.
3. **Only `secrets` split out.** The smallest change that isolates the
   vendor-dependent member. This is the hybrid the original question warned
   against: one tier organised by domain, the rest by kind.
4. **Keep the tier, drop the platform ExternalSecrets from its health check**
   (e.g. `wait: false` plus explicit `healthChecks` naming only the
   `Certificate` and the stores). Keeps one tier. Cost: the health
   definition becomes hand-maintained, and a stale platform secret becomes
   invisible to Flux.

## Leaning

Toward **option 2, but not yet**. The gating cost is real but so far
hypothetical: no ESO refresh failure has been observed, and the decision is
cheap to make later (three small entrypoint files, no manifest moves beyond
directory grouping). The trigger for deciding is the first time a
vendor-side failure blocks `apps` in practice, or a second cluster, whichever
comes first.

## Questions to answer before deciding

- How does ESO's Ready condition behave across a *transient* 1Password error?
  Does one failed refresh flip it, or only a sustained one? Observe it; do
  not assume.
- Does `apps` ever need to reconcile *during* a vendor outage? With a single
  operator, possibly never, which would make option 1 acceptable indefinitely.
