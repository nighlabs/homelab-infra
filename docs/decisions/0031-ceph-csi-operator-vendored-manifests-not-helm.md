# ADR-0031: ceph-csi-operator is installed from vendored manifests, not its Helm chart

- **Date:** 2026-09-06
- **Status:** Accepted
- **Supersedes / related:** implements the deployment half of
  [ADR-0006](0006-ceph-csi-external-proxmox-ceph.md); a third instance of
  [ADR-0020](0020-crd-tier-vendored-server-side-apply.md)'s admission rule;
  supporting evidence for [ADR-0029](0029-drop-helm-for-calico.md)

## Context

ADR-0006 chose ceph-csi via the ceph-csi-operator. The obvious delivery is the
upstream Helm chart, matching how nginx-gateway-fabric and cert-manager arrive
here: an `OCIRepository`/`HelmRepository` plus a `HelmRelease`.

That does not work, and the reason is not a preference.

The operator chart (v1.0.4) ships its CRDs as **ordinary templates**, and two
of them are enormous:

| Chart CRD template | Size | Client-side apply limit |
|---|---|---|
| `operatorconfig-crd.yaml` | 535,945 B | 262,144 B |
| `driver-crd.yaml` | 505,225 B | 262,144 B |

Each is roughly **double** the limit. helm-controller applies client-side and
cannot apply server-side, so the release fails on those two objects — the same
admission rule that already put Calico's and Gateway API's CRDs in the `crds/`
tier (ADR-0020).

The escape hatch that works for other charts is absent. Calico's chart and
cert-manager's chart both expose a values toggle to skip CRD installation; this
one does **not**. Both CRD templates are unconditional — the only template
expression in either file is a labels helper
(`{{- include "ceph-csi-operator.labels" . }}`), so there is no value to set and
no conditional to satisfy.

Upstream does publish plain manifests, in `deploy/multifile/`: `crd.yaml`
(1,055,174 B), `operator.yaml` (10,687 B) and `csi-rbac.yaml` (22,282 B).

## Decision

- **Do not use the Helm chart.** Install ceph-csi-operator from the upstream
  plain manifests, vendored at a pinned tag.
- `deploy/multifile/crd.yaml` → **`gitops/crds/ceph-csi/`**, where
  kustomize-controller applies it **server-side** and the size limit does not
  apply. `prune: false` and `wait: true` come from that tier.
- `operator.yaml` + `csi-rbac.yaml` → **`gitops/infrastructure/ceph-csi-operator/`**,
  retargeted from upstream's `ceph-csi-operator-system` to the `ceph-csi`
  namespace by a kustomize `namespace:` transformer.
- **The CRDs and the controller are bumped in the same commit.** The CRD schema
  and the controller that reads it are one unit; the regeneration `curl`
  commands live in the header of each directory's `kustomization.yaml`.
- **One namespace for the operator, the driver pods and the cephx Secrets.**

## Alternatives rejected

- **The Helm chart as-is.** Fails on two CRDs. Not a judgement call.
- **The chart with `crds.enabled=false`.** No such value exists — the templates
  are unconditional. Checked, not assumed.
- **The chart plus a post-renderer that strips the CRD manifests.** Would work
  mechanically, with the CRDs supplied from the `crds/` tier instead. Rejected
  as strictly worse than vendoring: it keeps the Helm machinery, a
  `HelmRepository` (this repo's first HTTP one — every other chart source is
  OCI), *and* the vendored CRDs, then adds a post-renderer whose silent failure
  mode is a chart upgrade re-introducing an object the renderer no longer
  matches. More moving parts for the same result.
- **A second HelmRelease for the CRDs with `dependsOn`.** ADR-0020 already
  established this does not work: helm-controller cannot server-side apply, so
  the size limit follows the CRDs wherever they are put.
- **Splitting the operator and the drivers across two namespaces**
  (upstream's `ceph-csi-operator-system` for the controller, `ceph-csi` for the
  drivers). Rejected: the operator creates the driver ServiceAccounts, prefixed
  `ceph-csi-operator-`, in whatever namespace the `Driver` CR lives in, and
  `csi-rbac.yaml` binds exactly those. Split them and the bindings name
  ServiceAccounts that do not exist there — the plugin pods then fail on RBAC
  rather than on Ceph, which is a misleading place to begin debugging.

## Consequences

- **1.0 MB of vendored CRD text in Git,** on top of Calico's 3 MB. This is the
  same complaint ADR-0020 leaves open ("render the CRDs at OCI build time so
  they stop living in Git"), and ceph-csi now strengthens the case. Unlike
  Calico's, this file has no Ansible-priming constraint — nothing primes
  ceph-csi before Flux — so it is the *easier* of the two to move to a
  build-time render if that work happens.
- **Bumps are manual and paired.** No Renovate datasource watches a vendored
  file the way it watches a chart `version:`. The regeneration commands are in
  the headers; the version appears in two directories and both must move
  together.
- **No `HelmRepository` in the repo.** Every chart source stays OCI.
- **The `namespace:` transformer is safe here only because
  `infrastructure/ceph-csi-operator/` contains nothing but core and RBAC
  kinds** — kustomize has schemas for those and rewrites `ClusterRoleBinding`
  subjects correctly. It would silently stamp a namespace onto any cluster-scoped
  CR added to that directory, the trap that forced Calico's BGP CRs into their
  own directory. Every ceph-csi CR therefore lives in `infrastructure-config/`.
  Verified after the change: the `Namespace` object renamed, all 9
  ServiceAccounts and every binding subject in `ceph-csi`.
- **This is the third occupant of the `crds/` tier,** which was introduced for
  what looked like a Calico-specific quirk. Three independent charts now hit the
  same 262,144-byte wall; the tier is load-bearing infrastructure, not a
  workaround.
- If ceph-csi-operator ever adds a `crds.enabled` toggle **and** a chart whose
  remaining objects fit, this decision is worth revisiting — as a new ADR.
