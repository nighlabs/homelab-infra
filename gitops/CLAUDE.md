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
  calico/                   #   calico (CNI + BGP) -> cert-manager ->
  kustomization.yaml        #   ceph-csi-operator -> ESO + Bitwarden SDK -> ...
                          #   (no metallb — Calico BGP owns LB, see below)
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

## Topology blinding — `${var}` placeholders, not SOPS (decided 2026-08-02)

Calico dodged this with `nodeAddressAutodetectionV4` (see above), but the BGP
CRs can't: a **peer IP and ASN have no autodetection equivalent**. The answer is
Flux post-build substitution, so nothing encrypted is ever committed:

```yaml
# infrastructure/calico/bgppeer.yaml — committed exactly like this
spec:
  peerIP: ${bgp_peer_ip}
  asNumber: ${bgp_peer_asn}
```
```yaml
# the deployment/ Kustomization that reconciles it
spec:
  postBuild:
    substituteFrom:
      - kind: Secret
        name: cluster-topology
```

`cluster-topology` is **Ansible-seeded at bootstrap** from the vault (it's
needed before ESO exists — see root `CLAUDE.md`). Values then rotate without a
commit, and diffs stay fully readable.

- **⚠ Undefined variables become the empty string and reconcile SUCCESSFULLY.**
  Per the docs: *"All the undefined variables in the format `${var}` will be
  substituted with an empty string unless a default value is provided."* A typo
  gives you `peerIP: ""`, applied and reported healthy. **Enable the
  kustomize-controller feature gate
  `--feature-gates=StrictPostBuildSubstitutions=true`.** `flux envsubst --strict`
  checks it locally / in pre-commit.
- `$${var}` escapes a literal; `$var` is untouched; substitution into a Secret
  needs `.stringData`. Disable per-resource with the annotation
  `kustomize.toolkit.fluxcd.io/substitute: disabled`.
- **Keep substituted resources out of kustomize `Components`** —
  [kustomize-controller#1506](https://github.com/fluxcd/kustomize-controller/issues/1506)
  reports flaky `substituteFrom` behavior there. (Components are the same blind
  spot for SOPS decryption, so the rule is just: plain `resources:` entries.)
- **When Ansible applies one of these, it must use `flux build kustomization
  --strict-substitute`, never `kustomize build`.** `postBuild` is a
  *kustomize-controller* feature; plain kustomize emits the literal `${var}`,
  which applies cleanly into a string field and silently breaks. Note `--dry-run`
  **skips** Secret/ConfigMap substitutions, so the Secret must exist and the
  build must have cluster access.
- **SOPS/age is the fallback, not the default** — reach for it only where
  substitution can't go (whole blocks/lists, or values needed at kustomize-*build*
  time). If it's ever needed: kustomize-controller decrypts non-Secret kinds too
  (the docs note `encrypted_regex` users "may wish to add other fields if you are
  encrypting other types of Objects"), but `apiVersion`/`kind`/`metadata` can
  never be encrypted. The age key arrives as Secret `sops-age` in `flux-system`,
  key file `age.agekey`, Ansible-seeded from BWS.

## Calico BGP — CRs, not Helm values

`values.yaml` stays the shared Helm values source. `BGPConfiguration`,
`BGPPeer`, and `BGPFilter` are **Calico CRs**, so they're plain manifests in
`infrastructure/calico/` referenced by its `kustomization.yaml` — they can't go
through `valuesFrom`.

- `values.yaml` moves to `bgp: Enabled` + **no encapsulation** (from
  `VXLANCrossSubnet`). Calico's VXLAN path uses no BGP at all, so this makes BGP
  load-bearing for pod routing — see `ansible/CLAUDE.md` §6 step 5 and §7 item 13.
- All nodes share one subnet, so the default **node-to-node mesh** distributes
  pod CIDRs with no `BGPPeer`. The pfSense peer is for LB advertisement only.
- `BGPFilter` (via `BGPPeer.spec.filters`) exports **only the LB range** and
  explicitly rejects the rest, so pfSense never learns the pod CIDR. Filters
  attach to `BGPPeer`s and the mesh isn't one, so the dataplane is unaffected.
  - **Not the only control.** pfSense carries an independent inbound prefix list
    permitting just the LB range (`ansible/CLAUDE.md` §7 item 8,
    `docs/pfsense-frr-bgp-setup.md` §6). The `BGPFilter` is enforced by the side
    we'd be guarding against misconfiguring, so it isn't trusted alone. If the
    pod CIDR ever shows up in `vtysh -c 'show ip bgp'`, **both** failed.
- **`BGPPeer` is Ansible-primed then Flux-adopted**, like Calico itself — the
  dataplane depends on it, so it can't wait for Flux.
- **`kube-controllers-ipamconfigs-rbac.yaml`** — the #12890 workaround. Also
  Ansible-primed, because LB allocation gates the Gateway → cert-manager → ESO
  chain, so a gap here stalls the bootstrap. Carries an explicit `REMOVE when
  fixed` comment; re-check on every Calico bump.

## Conventions

- Chart versions are pinned literally (Renovate bumps them); never `*`/floating.
- HelmRelease `releaseName` + namespace are explicit and stable — they're the
  adoption key when a release is Ansible-primed first.
- A controller that needs shared Helm values across Ansible + Flux uses the
  `values.yaml` + `configMapGenerator(disableNameSuffixHash)` + `valuesFrom`
  pattern above, so there's exactly one values source.

## Not here yet

- **Flux itself** (`FluxInstance`, secret-zero) — the `deployment/` entrypoints
  activate then. **Now sequenced *after* the Calico BGP migration** (see
  `ansible/CLAUDE.md` current-task banner). What Ansible seeds at that point:
  secret-zero, the `cluster-topology` Secret (post-build substitution), and —
  only if SOPS turns out to be needed — `sops-age`.
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
- Everything downstream of Calico: **Calico BGP** (no MetalLB), NGINX Gateway
  Fabric + cert-manager, ceph-csi-operator + StorageClasses, ESO + Bitwarden SDK
  Server, then the apps (Postgres/Redis → LiteLLM → Qdrant → RAG → Open WebUI →
  OTel). Order per `ansible/CLAUDE.md` §6 / design doc §6.
  - **Version:** the BGP work needs **`v3.32.1`** (v3.29.1 can't allocate
    LoadBalancer IPs at all). Bump `calico_version` and `helmrelease.yaml`'s
    `version:` together. **`kube-controllers-ipamconfigs-rbac.yaml` must ship
    alongside it** — 3.32's LB-IPAM RBAC grant is broken upstream
    ([#12890](https://github.com/projectcalico/calico/issues/12890)) and unfixed
    in 3.32.1. It's a temporary workaround with removal criteria: see
    `ansible/CLAUDE.md` §7 item 15.
