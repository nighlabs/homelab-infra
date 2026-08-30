# ADR-0016: Calico is installed once by Ansible, then adopted by Flux

- **Date:** 2026-07-08 (decided) · 2026-07-12 (Calico primed) · 2026-08-29 (adoption verified live)
- **Status:** Accepted
- **Supersedes / related:** [ADR-0008](0008-flux-via-flux-operator.md) (Flux), [ADR-0010](0010-calico-over-cilium.md) (Calico), [ADR-0021](0021-topology-blinding-postbuild-substitution.md) (the substitution rules the dual-apply must respect), [ADR-0029](0029-drop-helm-for-calico.md) (proposes dissolving the Helm half of this pattern). Code: `ansible/playbooks/bootstrap-cluster.yml`, `gitops/infrastructure/calico/`.

## Context

Flux's own pods need a CNI to run, but Calico — the CNI — is meant to be
Flux-managed. The chicken-and-egg is real: something has to install Calico
before Flux exists, and afterwards Flux has to own it without fighting whatever
installed it.

Two lifecycles are available on k3s for putting a manifest in place before the
cluster has a GitOps controller: k3s's own AddonManager autoload directory
(`/var/lib/rancher/k3s/server/manifests/`), or an out-of-band install from the
provisioning tool.

## Decision

**Ansible installs Calico exactly once during bootstrap; Flux then adopts the
same release.** The mechanism that makes the handoff quiet:

- **One pinned definition in Git, primed identically twice.**
  `gitops/infrastructure/calico/values.yaml` is the single source of Calico
  config. `bootstrap-cluster.yml` feeds it straight to `helm`
  (`values_files`); `kustomization.yaml`'s `configMapGenerator` turns the
  *same file* into the `calico-values` ConfigMap that `helmrelease.yaml` reads
  via `valuesFrom`. The Ansible-primed release and the Flux-managed release are
  byte-identical, so helm-controller adopts instead of fighting.
- **Adoption hinges on matching identity**: release name + namespace
  (`tigera-operator` / `tigera-operator`) and chart version must match what
  Ansible installed. `disableNameSuffixHash: true` on the generator is
  **required** — otherwise kustomize hashes the ConfigMap name and the fixed
  `valuesFrom` reference never resolves.
- **Fixed order:** k3s up → install Calico → wait for node Ready → Flux
  Operator + `FluxInstance`. A shared prerequisite (also needed for the Flux
  bootstrap) is fetching `/etc/rancher/k3s/k3s.yaml` over SSH and rewriting its
  `server:` to the DMZ IP so the control node has cluster access as soon as k3s
  is up.
- **The same pattern extends to anything the dataplane needs before Flux
  exists**: the `BGPPeer`, the #12890 RBAC workaround, and the
  `kubernetes-services-endpoint` ConfigMap are all Ansible-primed from their
  committed files and then Flux-adopted ([ADR-0018](0018-calico-bgp-replaces-metallb.md),
  [ADR-0024](0024-calico-ebpf-dataplane-no-kube-proxy.md)).

Two rules on the Ansible side of the dual-apply:

- **Keep the dual-applied set small.** It is: Calico's Helm values, the BGP
  CRs, and the Flux bootstrap objects. Everything else is Flux-only. `flux
  build` is the tool when you need it, not the default posture.
- **When Ansible must apply a substituted manifest, use `flux build
  kustomization --strict-substitute`, never `kustomize build`.** `postBuild`
  is a *kustomize-controller* feature; plain kustomize doesn't know `${var}`
  and applies the literal string into the cluster — it's a valid string field,
  so it *succeeds*. `flux build kustomization` runs the same implementation Flux
  uses. **Its trap:** per the docs, "variable substitutions from Secrets and
  ConfigMaps are skipped in dry-run mode" — so `--dry-run` silently drops
  exactly what you need. Order is: Ansible creates the Secret → `flux build
  kustomization` with cluster access → apply. One substitution source, two
  consumers, no reimplementation.

## Alternatives rejected

- **k3s autoload manifest** (`/var/lib/rancher/k3s/server/manifests/`) —
  rejected. k3s's AddonManager *continuously re-applies* autoloaded manifests,
  so Flux would fight it over the same objects. And deleting the manifest to
  "stop" autoload makes AddonManager **prune** — it tears Calico down. Clean
  only if k3s owns the CNI forever, which is the opposite of the goal.
- **Let Flux install Calico itself** — not possible: Flux's controllers are
  pods and need a CNI to be scheduled.
- **Two separate definitions (an Ansible one and a Flux one)** — rejected
  because any drift between them becomes a diff war on adoption. Hence the
  single `values.yaml` consumed by both sides.

## Consequences

- The Ansible prime leaves **no lingering reconciler**, so Flux's takeover is a
  quiet one-time adoption rather than an ongoing fight.
- The Calico release must be at **helm revision 1 with the live `Installation`
  CR matching `values.yaml` exactly** when Flux arrives. Re-check that state if
  the prime is ever re-run before Flux lands.
- `calico_version` (`group_vars/all/vars.yml`) and the `version:` in
  `helmrelease.yaml` must move in lockstep — both are the adoption key and both
  are Renovate-tracked.
- **Flatcar has no Python**, so anything that runs *on* the node (poll
  `/readyz`, read the kubeconfig) uses the `raw` module. The Helm / `k8s_info`
  work runs from `hosts: localhost` against the cluster via kubeconfig, so
  nothing k8s-side is installed on the node.
- The delicacy of Helm adoption (name/namespace/version must match exactly) is
  the main argument in [ADR-0029](0029-drop-helm-for-calico.md) for installing
  the operator from manifests instead — with server-side apply there is no
  release identity to match.

## Evidence

Verified live 2026-08-29 on a rolled-back `pre-flux-adoption` snapshot (a
genuine first bootstrap, not a re-run): helm `v1` superseded → `v2` deployed
with "Helm upgrade succeeded"; `Installation.spec` byte-identical to the
pre-run snapshot; zero pod restarts; the `BGPPeer` picked up
`kustomize.toolkit.fluxcd.io/name=infrastructure`. Re-proven on the
from-scratch `site.yml` run of 2026-08-30. See [`../worklog.md`](../worklog.md).
