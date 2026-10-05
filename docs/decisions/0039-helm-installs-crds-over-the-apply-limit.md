# ADR-0039: The 262144-byte limit is client-side apply's annotation; Helm can install CRDs larger than it

- **Date:** 2026-10-04
- **Status:** Accepted, verified (server dry-run) — applied to ESO; what it means for the existing `crds/` occupants is **not** decided here
- **Supersedes / related:** **corrects the stated mechanism** in [ADR-0020](0020-crd-tier-vendored-server-side-apply.md) and [ADR-0031](0031-ceph-csi-operator-vendored-manifests-not-helm.md). Their decisions stand and their tiers work, but the reason both give, that "helm-controller applies client-side, so a CRD over 262144 bytes cannot be a HelmRelease", is wrong. Related: [ADR-0029](0029-drop-helm-for-calico.md), whose supporting evidence cited ADR-0031; [ADR-0038](0038-three-vaults-platform-secrets-seed-and-sync.md) (the ESO milestone that surfaced this). Code: `gitops/infrastructure/external-secrets/helmrelease.yaml`.

## Context

The ESO chart's `secretstores` and `clustersecretstores` CRDs are ~374 KB
each. Under the admission rule ADR-0020 set and ADR-0031 applied, that would
send them to the vendored, server-side-applied `crds/` tier: a CRD over
262144 bytes "requires server-side apply, which helm-controller can't do".

Before vendoring a fourth set, the claim was tested rather than inherited.
ADR-0020's recorded failure (2026-08-02) was in fact *"ensure CRDs are
installed first"*: Calico v3.32 moved its CRDs out of the operator chart.
That is an install-contract change. The size explanation was attached
afterwards and never observed as a Helm failure.

## Decision

**The limit is on `metadata.annotations`, and the only thing that writes a
CRD-sized annotation is client-side `kubectl apply`**, whose
`last-applied-configuration` copy of the whole object is what overflows. Helm
creates objects without that annotation and patches them by three-way merge,
so the limit does not apply to a HelmRelease at all.

**So ESO installs its own CRDs from the chart** (`installCRDs: true`), with
`helm.sh/resource-policy: keep` so an uninstall can never delete them and
garbage-collect every ExternalSecret. ESO's CRDs are templates, not a `crds/`
directory, so each chart bump upgrades them with no regeneration step.

The `crds/` tier's admission rule becomes: **CRDs that cannot come from a
chart for a reason other than size.** Examples are no chart at all (Gateway
API's bundle belongs to none), an Ansible prime that must share the same file
(Calico, ADR-0016/0020), or a chart that cannot install cleanly for another
reason. Size alone no longer qualifies.

## Alternatives rejected

- **Vendor ESO's CRDs into `crds/` for consistency.** Adds ~1.1 MB to Git and
  a regeneration step on every bump, which Renovate will not do. The step
  would exist only to satisfy a rule whose premise is false.
- **Re-home Calico's and ceph-csi's CRDs into charts in the same change.**
  Out of scope: both tiers work and are verified, and each has its own
  considerations (the Calico prime; ceph-csi's chart has no CRD toggle, which
  is now harmless rather than blocking). They get their own decisions.

## Consequences

- **ADR-0020 and ADR-0031 keep their decisions, minus their size argument.**
  Calico's CRDs stay vendored because Ansible primes from the same file.
  Gateway API's stay because no chart owns them. ⚠ **ceph-csi's case is now
  open again**: ADR-0031's only blocker was size, so its operator chart may
  well install under Flux. Not acted on here; it needs its own record, with
  the same dry-run as evidence.
- **ADR-0029 loses one supporting argument** (ADR-0031's "precedent"). Its own
  case, dropping Helm to dissolve the Calico adoption problem, is unaffected.
- **Before vendoring any future CRD, test it** instead of measuring it:
  `kubectl create --dry-run=server -f <crd>` succeeds where
  `kubectl apply --dry-run=server -f <crd>` fails "Too long" for exactly this
  reason.

## Evidence

2026-10-04, against `testnode` (k3s v1.36.4), using the ESO 2.11.0 chart's
`secretstores.external-secrets.io` CRD (373,722 bytes as JSON). Server
dry-runs only; nothing was persisted:

| Command | Result |
|---|---|
| `kubectl create --dry-run=server -f ss-crd.yaml` (no annotation — Helm's path) | `created (server dry run)` |
| `kubectl apply --dry-run=server -f ss-crd.yaml` (client-side apply) | `metadata.annotations: Too long: may not be more than 262144 bytes` |

Live install through Flux is recorded in `../worklog.md` with the ESO
milestone.
