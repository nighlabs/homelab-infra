# CLAUDE.md — gitops/ (Flux-managed cluster contents)

Flux-managed cluster state. Bootstrapped by Ansible (see `ansible/CLAUDE.md` §6,
step 4) — the same run that provisions the nodes primes Calico and (next
milestone) installs the Flux Operator + `FluxInstance` pointed at this repo.
Standing repo-wide guardrails (no Terraform, no second Ceph, no CGNAT CIDRs, no
DHCP) apply here too — see the root `CLAUDE.md`.

## Layout (3-tier)

```
deployment/<cluster>/     # Flux entrypoints: the per-cluster Kustomization CRs
  infrastructure.yaml       #   -> ../../infrastructure  (controllers)
  apps.yaml                 #   -> ../../apps  (dependsOn infrastructure)
infrastructure/           # controllers, in dependency order (design §6):
  calico/                   #   calico (CNI) -> metallb -> cert-manager ->
  kustomization.yaml        #   ceph-csi-operator -> ESO + Bitwarden SDK -> ...
apps/                     # workloads only: litellm, qdrant, open-webui, ...
                          #   (empty until the infra layer is up)
```

- **`deployment/`** holds the Flux `Kustomization` CRs (the entrypoints Flux
  reconciles), NOT the workloads. One subdir per cluster (today: `snoop-a2o`).
  These reference the `flux-system` `GitRepository` created by the
  `FluxInstance` at Flux bootstrap — so they're **inert until that milestone**.
- **`infrastructure/` vs `apps/`**: controllers (CNI, LB, ingress, CSI, secrets)
  live in `infrastructure/`; only actual workloads go in `apps/`. `apps.yaml`
  `dependsOn` `infrastructure`, so nothing in `apps/` reconciles before the
  controllers are Ready.
- Ordering *within* a tier is by Flux `dependsOn` between HelmReleases /
  Kustomizations, matching the design-doc §6 dependency chain.

## Calico — the "Ansible primes, Flux adopts" pattern (live)

`infrastructure/calico/` is the first and reference example of the handoff
decided in `ansible/CLAUDE.md §6` (2026-07-08):

- **One pinned definition, primed identically twice.** `values.yaml` is the
  single source of Calico config. `bootstrap-cluster.yml` feeds it straight to
  `helm` (`values_files`); the `kustomization.yaml` `configMapGenerator` turns
  the *same file* into the `calico-values` ConfigMap that `helmrelease.yaml`
  reads via `valuesFrom`. So the Ansible-primed release and the Flux-managed
  release are byte-identical → helm-controller **adopts** instead of fighting.
- **Adoption hinges on matching identity**: release name + namespace
  (`tigera-operator` / `tigera-operator`) and chart version must match what
  Ansible installed. `disableNameSuffixHash: true` on the generator is
  **required** — otherwise kustomize hashes the ConfigMap name and the fixed
  `valuesFrom` reference never resolves.
- **No topology in Git**: Calico pins to `nodeAddressAutodetectionV4.kubernetes:
  NodeInternalIP` (which k3s advertises as the eth0/DMZ IP), so the real DMZ
  subnet stays vaulted and Calico can never land on the Ceph NIC (eth1). The pod
  CIDR (`10.42.0.0/16`) is the one network value committed — it's non-secret
  (already in the root `CLAUDE.md`) and `bootstrap-cluster.yml` asserts it equals
  `k3s_cluster_cidr` so `values.yaml` and the k3s config can't drift.
- **Version pins** (`calico_version` in `group_vars/all/vars.yml` and the
  `version:` in `helmrelease.yaml`) must stay in lockstep; both are
  Renovate-tracked.

## Conventions

- Chart versions are pinned literally (Renovate bumps them); never `*`/floating.
- HelmRelease `releaseName` + namespace are explicit and stable — they're the
  adoption key when a release is Ansible-primed first.
- A controller that needs shared Helm values across Ansible + Flux uses the
  `values.yaml` + `configMapGenerator(disableNameSuffixHash)` + `valuesFrom`
  pattern above, so there's exactly one values source.

## Not here yet

- **Flux itself** (`FluxInstance`, secret-zero) — next milestone; the
  `deployment/` entrypoints activate then.
- **TODO at Flux bootstrap — source is OCI, not Git.** The `deployment/`
  entrypoints currently point at a `GitRepository` named `flux-system` as a
  placeholder. When Flux is wired up, **rewrite them to an `OCIRepository`**
  source instead, so Flux pulls a versioned OCI **artifact** (the built gitops
  manifests) rather than reconciling straight from the Git branch. This needs:
  - **A GitHub Actions workflow** that builds the gitops tree into an OCI
    artifact and pushes it to a registry (e.g. GHCR) — on merge to `main`
    and/or tag (`flux push artifact oci://…` or the equivalent action).
  - **Image/artifact signing** (cosign — keyless via the GHA OIDC identity is
    the low-friction path) so the artifact is provably built by our pipeline,
    plus **`OCIRepository.spec.verify`** (cosign) on the Flux side so
    source-controller **refuses to reconcile an unsigned/forged artifact** — a
    malicious actor pushing a look-alike image to the registry can't get it
    applied to the cluster. Update the `FluxInstance` bootstrap (Ansible) to
    provision the registry pull creds + the cosign trust config alongside
    secret-zero.
- Everything downstream of Calico: MetalLB, NGINX Gateway Fabric + cert-manager,
  ceph-csi-operator + StorageClasses, ESO + Bitwarden SDK Server, then the apps
  (Postgres/Redis → LiteLLM → Qdrant → RAG → Open WebUI → OTel). Order per
  `ansible/CLAUDE.md` §6 / design doc §6.
