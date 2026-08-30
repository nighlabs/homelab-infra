# ADR-0028: Flux consumes a keyless-cosign-signed OCI artifact via an `OCIRepository` with `spec.verify`; the FluxInstance is sync-less; Ansible seeds the OCIRepository + root Kustomization, committed inside the path they reconcile

- **Date:** 2026-08-29 (decided; CI workflow live) · 2026-08-30 (merged, verified live on a from-scratch `site.yml` run)
- **Status:** Accepted
- **Supersedes / related:** [ADR-0008](0008-flux-via-flux-operator.md) (Flux via the operator — still true; the operator's *sync shortcut* is what's declined), [ADR-0016](0016-calico-ansible-primes-flux-adopts.md) (the same primes/adopts handoff, applied to Flux's own root), [ADR-0021](0021-topology-blinding-postbuild-substitution.md) (the strict gate is applied through the FluxInstance), [ADR-0020](0020-crd-tier-vendored-server-side-apply.md) (the artifact could carry the CRDs). Code: `.github/workflows/gitops-artifact.yml`, `gitops/deployment/homelab/source.yaml` + `sync.yaml` + `kustomization.yaml`, `ansible/playbooks/flux-bootstrap.yml`, `ansible/playbooks/tasks/flux-bootstrap-cluster.yml`.

## Context

The first Flux bootstrap (2026-08-29) used the flux-operator's `spec.sync`
shortcut: the `FluxInstance` generated a `GitRepository` + root `Kustomization`
pointed at this repo's `main`. That works, but it means **anything that can
push to `main` reaches the cluster**, with no verification that the content
came from this repo's CI. The goal was signature-gated delivery: the cluster
should refuse to reconcile anything not signed by this repo's own workflow
identity.

The sequence was planned as steps 1→2→4→3: bootstrap Flux (Git source), verify
adoption, add the CI artifact build + signing, then switch the source.

## Decision

**CI builds `gitops/` into an OCI artifact and cosign-signs it keyless via the
GitHub Actions OIDC identity. The cluster consumes it through an
`OCIRepository` carrying `spec.verify` + `matchOIDCIdentity`, so
source-controller refuses to reconcile an unsigned or forged artifact. The
`FluxInstance` is deliberately left without `spec.sync`; Ansible seeds the
`OCIRepository` + root `Kustomization` from their committed files, and those
files live inside the path the root reconciles, so Flux adopts them and
thereafter drift-corrects them itself.**

- **`spec.sync` cannot express verification.** It accepts `OCIRepository` as a
  kind but exposes only 8 fields (`interval`, `kind`, `name`, `path`,
  `provider`, `pullSecret`, `ref`, `url`) and **no `verify`**. Confirmed
  against the live CRD *and* the operator's `main`, where the `Sync` struct is
  unchanged and no issue tracks adding it — a scope boundary, not a version
  gap. Wiring OCI through `spec.sync` would have *looked finished* while the
  signature gate was silently absent — the same class of failure as an
  unresolved `${var}` reconciling green.
- **Keyless over a key.** A long-lived cosign key is one more BWS secret to
  store, rotate and leak. Keyless + `matchOIDCIdentity` (issuer + subject
  regex) asserts *"signed by this workflow, in this repo"* — a stronger claim
  than *"signed by a key someone holds"* — with nothing at rest.
- **Self-management is a LAYOUT property, not an object property.** Confirmed
  in `flux2`'s `pkg/manifestgen/sync/sync.go`: `flux bootstrap` writes the
  source+root manifest to `<TargetPath>/<ns>/gotk-sync.yaml` while setting that
  Kustomization's `spec.path` to `./<TargetPath>` — the root reconciles a
  directory containing its own definition. We reproduce that deliberately:
  `source.yaml` and `sync.yaml` are listed in
  `deployment/homelab/kustomization.yaml`, which is what `sync.yaml` reconciles.
  Lay it out any other way and nothing heals the root.
- **The operator still earns its place sync-less.** Sync is 2 of the 34
  objects in its inventory; the other 32 are the whole Flux install (11 CRDs,
  4 controller Deployments, RBAC, NetworkPolicies, ResourceQuota, namespace).
  It still pins and auto-upgrades the distribution within `2.9.x` (the
  k3s-sysupdate posture: patches unattended, minors deliberate), applies the
  `StrictPostBuildSubstitutions` patch via `spec.kustomize.patches`, and
  drift-corrects those 32 objects. We are declining the bootstrap shortcut,
  not the lifecycle manager.
- **Phase 2 of `flux-bootstrap.yml` is gone**, along with its `when: not
  exists` guard — with no `spec.sync` there is nothing for a re-run to strip.
  The play is idempotent: re-seeding the `OCIRepository` + root Kustomization
  from their committed files is an adoption, not a recreate.
- **The artifact's mutable `latest` tag is moved only AFTER cosign-signing the
  digest** (see the workflow header for why that is deliberate and safe). To
  freeze on a known-good artifact, replace `tag:` with `digest: sha256:…`.
  `--reproducible` stabilises the *layer* digest, not the manifest digest —
  `org.opencontainers.image.revision` embeds the commit SHA, so every build
  mints a new manifest digest and therefore a new OCIRepository revision; that
  is why the workflow negates `gitops/**/*.md` in its trigger `paths` rather
  than relying on the ignore list alone.
- **No pull secret** — the package is public (anonymous pull works), one less
  bootstrap-tier secret than expected. If the repo/package ever goes private,
  add a BWS secret and set `spec.secretRef`.

## Alternatives rejected

- **`spec.sync` with `kind: OCIRepository`** — cannot express `verify`, as
  above.
- **Patching the generated source** — the operator holds those objects in its
  `status.inventory` (34 entries) and reconciles them, so an in-place edit is
  reverted on the next pass.
- **GitHub artifact attestations as the gate** —
  `OCIRepository.spec.verify.provider` is an enum of exactly `cosign`/`notation`;
  there is no attestation provider, so attestations **cannot** gate
  reconciliation. They compose fine alongside cosign as extra SLSA provenance
  for humans and `gh attestation verify`, but they are not admission control
  and must not be mistaken for it — do not swap one for the other.
- **A long-lived cosign key** — one more secret to store/rotate/leak, for a
  weaker claim.
- **Keeping the GitRepository source and adding verification there** — Git
  commit-signature verification would gate on *who committed*, not on *CI
  built this*; the artifact is what the cluster runs.
- **Converting a live sync-based FluxInstance in place** — **the play refuses
  to do this.** Stripping `spec.sync` prunes the generated `flux-system`
  Kustomization, whose `prune: true` cascades to all three tiers and
  `infrastructure` pruning its inventory **uninstalls Calico**. Migration is by
  **re-provision**, per the disposable-cluster rule.

## Consequences

- **(a) Pushing to `main` no longer reaches the cluster on its own** — only a
  signed artifact does, so the workflow is merge → CI signs → Flux picks it
  up. The artifact must be **rebuilt from `main`** before a bootstrap runs,
  since Flux pulls the artifact, not the branch.
- **(b) The self-managed source can disable its own verification** if a future
  signed artifact drops `verify`. Not a hole — the artifact must already be
  signed by our workflow identity to apply — but `source.yaml` warrants
  CI-secret-level scrutiny, not routine review.
- **(c) A bad committed root is self-inflicted lockout**, recovered only by
  re-running `flux-bootstrap.yml` — which is why that play must stay
  idempotent and re-runnable rather than one-shot.
- **⚠ The artifact root is `gitops/` itself, so the `./gitops` prefix is
  gone.** Paths inside it are `deployment/homelab/…`, `infrastructure/…`,
  `crds/…`. Every tier path lost its prefix and the root sync path is
  `./deployment/homelab`. The failure this prevents — source Ready,
  Kustomization failing "kustomization path not found" — is the one already
  burned into this repo's history (Failure 2 of the first bootstrap: the
  rename to `homelab` was committed nowhere, and every local check sailed past
  it because `kubectl kustomize` proves the build works on disk and says
  nothing about what Flux pulls). Keep the prefix off any new tier.
- **Flux reads the remote/artifact, not your working tree.** The play
  preflights that the sync path exists in what Flux will pull, and warns when
  `gitops/` has uncommitted changes.
- **The `flux-system` name is kept** for the OCIRepository and root
  Kustomization so the three tier entrypoints written against the
  operator-generated source keep resolving without change.
- **The operator is Ansible-owned and is NOT primed-for-adoption.** Nothing
  in `gitops/` manages the flux-operator. Self-management of the operator (a
  HelmRelease reconciled by the Flux it installed) is a real pattern and a
  real footgun — an in-flight upgrade can delete the controller performing it.
  Adopt it deliberately or not at all; don't drift into it.
- **The `--feature-gates` assert folds, it does not count.** The operator
  already emits `--feature-gates=ObjectLevelWorkloadIdentity=false`, so
  appending ours yields two `--feature-gates` arguments. "A repeated flag
  overrides, last one wins" is **false**: `--feature-gates` is a component-base
  `MapStringBool` — the first `Set()` clears defaults, later ones **merge**;
  kustomize-controller's own startup log settles it. So appending is correct
  and version-proof, and the assert folds every `--feature-gates` argument into
  an effective map (last writer wins *per key*) and checks the resulting
  value — exercised against six cases including one-combined-flag and
  same-key-twice.
- **Rolling back a Proxmox snapshot is the RIGHT way to get back to a pre-Flux
  cluster. Deleting the FluxInstance is NOT** — it prunes the generated
  `flux-system` Kustomization and the cascade uninstalls Calico. ⚠ The
  mechanism is the **operator's own inventory + a
  `fluxcd.controlplane.io/finalizer`**, NOT Kubernetes ownerReferences — the
  generated objects carry *no* ownerReferences at all (verified 2026-08-29).
  So `kubectl delete --cascade=orphan` does **not** save you: it defeats GC,
  and GC is not what is doing the deleting. A rollback with RAM also preserves
  the kubeconfig and the `cluster-topology` Secret, so `bootstrap-cluster.yml`
  need not re-run.
- **The play needs no credentials** — no BWS, no Keychain prompt. Every value
  is a committed constant or comes from the cluster via the kubeconfig. It
  requires `bootstrap-cluster.yml` to have run first (kubeconfig +
  `cluster-topology`), asserted with messages that say so.
- Pins: **flux-operator chart `0.58.0`** (⚠ no leading `v`; OCI chart tags are
  bare semver while the GitHub release is `v0.58.0`) installing **Flux
  `2.9.x`** — minor pin, patches automatic.
- The open follow-on from [ADR-0020](0020-crd-tier-vendored-server-side-apply.md)
  (render the Calico CRDs into the artifact instead of vendoring) becomes
  possible now that CI builds the artifact.

## Evidence

Step 4 (2026-08-29): `.github/workflows/gitops-artifact.yml` publishes
`ghcr.io/nighlabs/homelab-infra/gitops`; `cosign verify` against
`--certificate-identity-regexp='^https://github.com/nighlabs/homelab-infra/'`
+ the GitHub OIDC issuer passes (claims validated, transparency-log entry
confirmed, cert chained to a trusted CA); the pulled layer contains all 21
manifests and zero markdown.

Step 3 (2026-08-30, from-scratch `site.yml` run, verified against the live
cluster rather than inferred from a zero exit): source is
`OCIRepository/flux-system`, **zero `GitRepository` exists**;
`SourceVerified=True :: verified signature of revision latest@sha256:ff22…`;
the seeded `OCIRepository` and `BGPPeer` both carry
`kustomize.toolkit.fluxcd.io/name` labels (Flux drift-corrects the very
objects Ansible seeded); all four Kustomizations Ready at the same OCI digest;
Flux v2.9.4. That `SourceVerified=True` row on an OCI source with no
`GitRepository` is the decision's entire thesis, proven on a clean provision.
See [`../worklog.md`](../worklog.md).
