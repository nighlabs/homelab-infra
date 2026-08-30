# ADR-0020: A `crds/` tier — Calico's CRDs are vendored and server-side applied by a plain Kustomization, `prune: false`, `wait: true`

- **Date:** 2026-08-02 (decided, hit for real during the v3.32.1 bump)
- **Status:** Accepted — the build-time-render follow-on is **open**
- **Supersedes / related:** [ADR-0019](0019-k3s-1.36-calico-3.32.1-version-pair.md) (the bump that surfaced it), [ADR-0016](0016-calico-ansible-primes-flux-adopts.md) (one source, primed twice), [ADR-0028](0028-gitops-delivery-signed-oci-syncless-fluxinstance.md) (the OCI artifact that could carry the CRDs instead of Git), [ADR-0029](0029-drop-helm-for-calico.md). Code: `gitops/crds/`, `gitops/deployment/homelab/crds.yaml`, `ansible/playbooks/bootstrap-cluster.yml`.

## Context

Bumping `calico_version` to `v3.32.1` made the helm prime fail with *"no matches
for kind Installation / APIServer / Goldmane / Whisker in version
operator.tigera.io/v1 — ensure CRDs are installed first."*

**Calico v3.32 removed the CRDs from the tigera-operator chart.** Its `crds/`
dir is empty (v3.29.1 shipped 5 files); they moved to a separate
`crd.projectcalico.org.v1` chart. Upstream's reason: Helm never upgrades or
deletes CRDs living in a chart's `crds/`, so they were split out to get a real
lifecycle. **This is an install-contract change, not a bad pin** — it applies
to any Calico ≥3.32.

## Decision

**A new `gitops/crds/` tier, reconciled by its own Flux `Kustomization`
(`crds`) that `infrastructure` `dependsOn`. Calico's CRDs are vendored into
`crds/calico/crds.yaml` (3.0 MB of generated output, regenerated — never
hand-edited — on version bumps via the command in its header) and applied
server-side.**

- **kustomize-controller applies server-side by default**, so a plain
  `Kustomization` over vendored YAML works where a HelmRelease cannot (below).
- **One source, primed twice.** `bootstrap-cluster.yml` applies **that same
  file** with `kubernetes.core.k8s` + `server_side_apply`. Do not "simplify" it
  back to rendering from the chart: that recreates two sources that drift apart
  the moment the vendored copy is regenerated — the identical failure mode the
  shared `values.yaml` exists to prevent.
- **`prune: false` on the `crds` Kustomization is deliberate.** Pruning a CRD
  cascades — Kubernetes garbage-collects every CR of that kind, which for
  Calico is the entire network config (Installation, IPPools, BGP). A rendering
  slip that dropped a CRD from the build would take the dataplane with it.
  Removing a CRD is a manual act, never a reconcile.
- **`wait: true`** so `infrastructure`'s `dependsOn: [crds]` gates on the CRDs
  being *Established*, not merely submitted. Timeout 10m — 3 MB server-side
  applied is tight on a cold API server, and a timeout here blocks everything.
- **No `postBuild` on the `crds` tier, deliberately.** kustomize-controller
  only runs substitution when `spec.postBuild` is set, so leaving it off is
  what keeps 3 MB of generated CRD text — descriptions, examples, regex
  patterns, anything that might contain a `${...}` — from being scanned and,
  under the strict gate ([ADR-0021](0021-topology-blinding-postbuild-substitution.md)),
  hard-failing the one tier everything else depends on. This tier has no
  topology in it and needs none.
- **Calico is the exception, not a convention.** Don't route other
  controllers' CRDs here just because they have some; the tier exists for CRDs
  a chart can't install itself *and* that need server-side apply.

## Alternatives rejected

- **A second HelmRelease for the CRD chart, with `dependsOn`** — the obvious
  fix, and it fails. **3 of the 32 CRDs exceed the 262144-byte client-side
  apply limit** (`installations` 1.46 MB, `gatewayapis` 466 KB, `istios`
  284 KB), so they require server-side apply. helm-controller drives Helm's
  client-side apply; the chart's own README says to use `helm template |
  kubectl apply --server-side` for exactly this reason. So the CRD chart can
  never be a HelmRelease — in Flux *or* in Ansible.
- **Render from the chart at prime time (Ansible) and vendor for Flux** — two
  sources that drift; rejected for the reason above.
- **`prune: true` for consistency with the other tiers** — rejected; the
  cascade would delete the dataplane's config on a rendering slip.

## Consequences

- **On every `calico_version` bump:** regenerate `crds.yaml` in the same commit
  as `vars.yml` + `helmrelease.yaml`, and apply CRDs **before** the operator
  chart — upstream is explicit that Helm won't do it for you.
- **Accepted, knowingly:** 3.0 MB of generated output committed to Git, and
  every bump adds another full copy to history forever. Accepted on 2026-08-02
  because it is the only option that gives Flux the ordering guarantee today.
- **⚠ Open follow-on — render the CRDs at OCI build time and stop vendoring
  them.** Now that the artifact is built by CI
  ([ADR-0028](0028-gitops-delivery-signed-oci-syncless-fluxinstance.md)), the
  workflow could run `helm template calico-crds crd.projectcalico.org.v1
  --version <pin>` into the artifact instead, so the 3 MB exists in the OCI
  layer and never in Git. **Two constraints that must survive the move:**
  (a) the version must come from the *same* pin as `calico_version` /
  `helmrelease.yaml`, or the three drift silently; (b) `bootstrap-cluster.yml`
  primes from the vendored file — if Git stops carrying it, Ansible needs its
  own render at that same pin, and the "one source, primed twice" invariant has
  to be re-established some other way (a CI-published artifact both sides
  consume, most likely). **Don't delete the vendored file until that second
  half is actually solved**, and don't fold this into the next-stack milestone
  — it's its own step.
- [ADR-0029](0029-drop-helm-for-calico.md) notes that
  `manifests/operator-crds.yaml` is byte-for-byte identical to the chart's
  template output, so regeneration would become a `curl` under that proposal.

## Evidence

The failure was hit for real on 2026-08-02 on the first bootstrap at the new
pin; the tier resolved it and has reconciled `Ready` on every bootstrap since,
including the from-scratch `site.yml` run of 2026-08-30. See
[`../worklog.md`](../worklog.md).
