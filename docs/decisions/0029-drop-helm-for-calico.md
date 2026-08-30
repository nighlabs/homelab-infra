# ADR-0029: Install the tigera operator from manifests instead of the Helm chart

- **Date:** 2026-08-02 (proposed, while fixing the v3.32 CRD split)
- **Status:** **Proposed** — deliberately not yet done
- **Supersedes / related:** would rework [ADR-0016](0016-calico-ansible-primes-flux-adopts.md) (removes the Helm-adoption half of the pattern), [ADR-0020](0020-crd-tier-vendored-server-side-apply.md) (CRD regeneration becomes a `curl`), [ADR-0019](0019-k3s-1.36-calico-3.32.1-version-pair.md). Code that would change: `gitops/infrastructure/calico/` (`helmrepository.yaml`, `helmrelease.yaml`, `kustomization.yaml`), `ansible/playbooks/bootstrap-cluster.yml`, `ansible/requirements.yml`, `ansible/README.md` prerequisites.

## Context

Investigated while fixing the v3.32 CRD split. Calico supports a manifest
install alongside the chart; neither is deprecated. The "Ansible primes, Flux
adopts" pattern for Calico works, but its Helm half is delicate: release name,
namespace and chart version must match exactly or helm-controller fights the
primed release.

## Decision (proposed)

Install the tigera operator from Calico's published manifests, server-side
applied by both Ansible and Flux; drop the Helm chart, the HelmRelease, and
the shared-values indirection.

- **The sizes make the case.** `manifests/tigera-operator.yaml` is **19.6 KB**
  — Namespace, ServiceAccount, 2 ClusterRoles, bindings, one Deployment.
  `manifests/operator-crds.yaml` is the 32 CRDs and is **byte-for-byte
  identical content** to what `helm template crd.projectcalico.org.v1`
  produces (verified: 40,019 lines each, `diff` clean), so `gitops/crds/`
  needs **no rework** — regeneration just becomes a `curl`.
- **We use nothing the chart provides.** `bgp`, `ipPools`,
  `nodeAddressAutodetectionV4`, `linuxDataplane` all live in the
  **`Installation` CR**; the chart's only job is passing `installation:`
  straight through to it.
- **What it deletes:** `helmrepository.yaml`, `helmrelease.yaml`, the
  `configMapGenerator` + `disableNameSuffixHash` + `valuesFrom` indirection,
  the `helm` binary as a control-node prerequisite, `kubernetes.core.helm`, the
  Helm-4-vs-`kubernetes.core` 6.x compatibility pin in `requirements.yml`, and
  helm-controller from Calico's dependency chain.
- **The big one: it dissolves the adoption problem.** With manifests there is
  no release identity to match; both sides server-side apply the same YAML,
  idempotent by construction. **And the HelmRelease is the only thing in the
  repo that needs adoption** — `BGPPeer`, the #12890 workaround, the endpoint
  ConfigMap, and now the Flux root are already plain Ansible-primed manifests;
  everything after Flux exists needs no priming. So this removes the *concept*
  rather than leaving a special case. `gitops/CLAUDE.md`'s "reference example
  of the handoff" framing would need rewriting, not patching.

## Alternatives rejected (so far)

- **Keep the chart** — the current state; works, at the cost of the
  adoption-key fragility above and the Helm-version coupling on the control
  node.

## Consequences

- **What we'd give up:** chart knobs for the *operator deployment* (registry,
  pull secrets, tolerations, resources) become kustomize patches — rare here;
  and Renovate ergonomics, since a chart `version:` is first-class while a
  vendored manifest needs a regex manager on a pinned version string (already
  true for `crds.yaml` regardless).
- **⚠ Sequencing: not now.** At proposal time the bootstrap had only just been
  unblocked and the 1.36/3.32.1 rebuild was unverified; landing this on top
  would put two variables in flight — the same attribution argument accepted
  for the eBPF staging ([ADR-0024](0024-calico-ebpf-dataplane-no-kube-proxy.md)).
  It is also *cheaper* afterwards, with a known-good cluster to diff against.
  Since then the Flux bootstrap and the OCI source have both landed and been
  verified, so the "known-good cluster to diff against" now exists.
- If adopted, `calico_version` would still need to move in lockstep with the
  manifest URL pin and the vendored CRDs.

## Evidence

None yet — proposed only. The size/identity checks above were done on
2026-08-02.
