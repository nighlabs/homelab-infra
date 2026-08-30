# ADR-0008: GitOps: FluxCD, installed by the Flux Operator, bootstrapped by Ansible as the last provisioning step

- **Date:** 2026-07 (initial design); bootstrap verified live 2026-08-29
- **Status:** Accepted (the *source* detail below is superseded by [ADR-0028](0028-gitops-delivery-signed-oci-syncless-fluxinstance.md))
- **Supersedes / related:** [ADR-0007](0007-ansible-not-terraform.md); [ADR-0016](0016-calico-ansible-primes-flux-adopts.md) (the one thing Flux must adopt rather than install); [ADR-0028](0028-gitops-delivery-signed-oci-syncless-fluxinstance.md); `../architecture.md` §3.7; `gitops/CLAUDE.md`

## Context

Everything in the cluster that is not the CNI should arrive from Git, in
dependency order, with no hand-applied state. The GitOps controller has to be
manageable purely declaratively, and it has to be installed by the same
Ansible run that builds the VMs, so a from-scratch rebuild ends with a cluster
that is already reconciling.

## Decision

- **FluxCD.** All workloads arrive as Flux `HelmRelease`s / `Kustomization`s
  with `dependsOn` ordering (CRDs → infrastructure → apps).
- **Installed via the Flux Operator** (the declarative `FluxInstance` CRD),
  which pins and auto-upgrades the Flux distribution within a minor and
  drift-corrects the Flux install itself.
- **Bootstrapped by Ansible as the final provisioning step.** The same
  playbook run that builds the VMs installs the operator, applies one
  `FluxInstance`, and steps aside. After that, changes arrive through Git, not
  through Ansible.

The original design had the `FluxInstance`'s `spec.sync` point Flux at the Git
repo directly, with a deploy key read from Bitwarden Secrets Manager. That
detail changed on 2026-08-29: Flux now consumes a **cosign-signed OCI
artifact** built by CI, and the `FluxInstance` is deliberately **sync-less**.
The reasoning is in [ADR-0028](0028-gitops-delivery-signed-oci-syncless-fluxinstance.md);
this ADR stands for the choice of Flux and the bootstrap-by-Ansible shape.

## Alternatives rejected

- **ArgoCD.** Heavier, and its Application/AppProject layer plus ConfigMap-
  driven configuration is fiddly to manage purely declaratively (prior
  experience). Flux's CRD-native model fits "manifests all the way down."
- **k3s's own Helm controller / autoload manifests.** Disabled
  (`disable-helm-controller: true`) — Flux owns Helm, and k3s's AddonManager
  would fight Flux over any object both claimed (see
  [ADR-0016](0016-calico-ansible-primes-flux-adopts.md)).
- **`flux bootstrap` CLI.** Writes into the repo and needs a Git credential at
  bootstrap; the operator's `FluxInstance` is a single CR Ansible can apply and
  assert against, with nothing written back to Git.

## Consequences

- The Flux bootstrap play **requires** the cluster-bootstrap play to have run
  first: it needs that play's kubeconfig and its `cluster-topology` Secret. It
  needs **no credentials** itself — everything it uses is a committed constant
  or comes from the cluster.
- The operator is **Ansible-owned and not primed-for-adoption**: nothing in
  `gitops/` manages the flux-operator, so there is no second writer. Self-
  management of the operator (a HelmRelease for the operator, reconciled by the
  Flux it installed) is a real pattern and a real footgun — an in-flight
  upgrade can delete the controller performing it. Adopt it deliberately or
  not at all.
- Flux's controllers run **cluster-admin**. For a repo whose job is installing
  a CNI, server-side-applying CRDs, and managing cluster-scoped Calico CRs,
  that is close to the genuine requirement; revisit when `apps/` has workloads
  worth isolating from the infra tier.
- Flux is **in-cluster** and authenticates with its own ServiceAccount token.
  It never reads a kubeconfig; "a scoped kubeconfig for Flux" is a category
  error and must not be re-raised as a bootstrap blocker (`../worklog.md`,
  2026-08-17).
- The kustomize-controller runs with `StrictPostBuildSubstitutions=true`,
  applied through the `FluxInstance` and asserted by the play, because of
  [ADR-0021](0021-topology-blinding-postbuild-substitution.md).
