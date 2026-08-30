# CLAUDE.md — gitops/ (Flux-managed cluster contents)

Flux-managed cluster state. Bootstrapped by Ansible (see `ansible/CLAUDE.md` §6,
step 4) — the same run that provisions the nodes primes Calico, then
`playbooks/flux-bootstrap.yml` installs the flux-operator + a `FluxInstance`
pointed at this repo. Standing repo-wide guardrails (no Terraform, no second
Ceph, no CGNAT CIDRs, no DHCP) apply here too — see the root `CLAUDE.md`.

## Layout (4-tier)

```
deployment/<cluster>/     # Flux entrypoints: the per-cluster Kustomization CRs
  crds.yaml                 #   -> ../../crds  (prune: false, wait: true)
  infrastructure.yaml       #   -> ../../infrastructure  (dependsOn crds)
  apps.yaml                 #   -> ../../apps  (dependsOn infrastructure)
crds/                     # CRDs that must be Established BEFORE controllers:
  calico/                   #   vendored, server-side applied (see below)
infrastructure/           # controllers, in dependency order (design §6):
  calico/                   #   INSTALLS calico (operator chart + values)
  calico-bgp/               #   CONFIGURES it: BGP CRs, LB IPAM pool, #12890 RBAC
  kustomization.yaml        #   then cert-manager -> ceph-csi-operator ->
                          #   ESO + Bitwarden SDK -> ...
                          #   (no metallb — Calico BGP owns LB, see below)
apps/                     # workloads only: litellm, qdrant, open-webui, ...
                          #   (empty until the infra layer is up)
```

- **`crds/`** is the tier added 2026-08-02 for CRDs a chart can't install
  itself. **Only Calico qualifies today, and it's the exception, not a
  convention** — don't route other controllers' CRDs here just because they have
  some. See "The CRD tier" below for why Calico is special.
- **`deployment/`** holds the Flux `Kustomization` CRs (the entrypoints Flux
  reconciles), NOT the workloads. One subdir per cluster, and **the directory
  name IS the cluster key from `ansible/inventory/nodes.yml`** — today `homelab`
  (renamed from `snoop-a2o`, a node name, on 2026-08-29). That is not cosmetic:
  `flux_sync_path` derives the FluxInstance's sync path as
  `gitops/deployment/{{ cluster_name }}`, so a rename here silently points Flux
  at a path that doesn't exist — and the failure is asymmetric, because the
  `GitRepository` still goes Ready (the clone worked) while only the
  `flux-system` Kustomization fails. These reference the `flux-system`
  `GitRepository` the `FluxInstance` generates from `spec.sync`.
  - **`infrastructure` and `apps` carry `postBuild.substituteFrom` on the
    Ansible-seeded `cluster-topology` Secret; `crds` deliberately does not.**
    kustomize-controller only runs substitution when `spec.postBuild` is set, so
    leaving it off `crds` is what keeps 3 MB of generated CRD text from being
    scanned — and, under the strict gate, from hard-failing the one tier
    everything else `dependsOn`. `apps` has no placeholders yet and gets the
    block anyway, so the first one added is covered by the gate rather than
    silently substituting to `""`.
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

## The CRD tier — why Calico's CRDs are vendored (decided 2026-08-02)

Hit for real when bumping `calico_version` to `v3.32.1`: the helm prime failed
with *"no matches for kind Installation / APIServer / Goldmane / Whisker in
version operator.tigera.io/v1 — ensure CRDs are installed first."*

- **Calico v3.32 removed the CRDs from the tigera-operator chart.** Its `crds/`
  dir is empty (v3.29.1 shipped 5 files); they moved to a separate
  `crd.projectcalico.org.v1` chart. Upstream's reason: Helm never upgrades or
  deletes CRDs living in a chart's `crds/`, so they were split out to get a real
  lifecycle. **This is an install-contract change, not a bad pin** — it applies
  to any Calico ≥3.32.
- **⚠ A second HelmRelease with `dependsOn` does NOT work** — the obvious fix,
  and it fails. **3 of the 32 CRDs exceed the 262144-byte client-side apply
  limit** (`installations` 1.46 MB, `gatewayapis` 466 KB, `istios` 284 KB), so
  they require **server-side apply**. helm-controller drives Helm's client-side
  apply; the chart's own README says to use `helm template | kubectl apply
  --server-side` for exactly this reason. So the CRD chart can never be a
  HelmRelease — in Flux *or* in Ansible.
- **kustomize-controller applies server-side by default**, so a plain
  `Kustomization` over vendored YAML does work. Hence `crds/calico/crds.yaml`:
  3.0 MB of generated output, regenerated (never hand-edited) on version bumps
  via the command in its header.
- **One source, primed twice** — `bootstrap-cluster.yml` applies **that same
  file** with `kubernetes.core.k8s` + `server_side_apply`. Do not "simplify" it
  back to rendering from the chart: that recreates two sources that drift apart
  the moment the vendored copy is regenerated, which is the identical failure
  mode the shared `values.yaml` exists to prevent.
- **⚠ `prune: false` on the `crds` Kustomization is deliberate.** Pruning a CRD
  cascades — Kubernetes garbage-collects every CR of that kind, which for Calico
  is the entire network config (Installation, IPPools, BGP). A rendering slip
  that dropped a CRD from the build would take the dataplane with it. Removing a
  CRD is a manual act, never a reconcile.
- **`wait: true`** so `infrastructure`'s `dependsOn: [crds]` gates on the CRDs
  being *Established*, not merely submitted.
- **On every `calico_version` bump**: regenerate `crds.yaml` in the same commit
  as `vars.yml` + `helmrelease.yaml`, and apply CRDs **before** the operator
  chart — upstream is explicit that Helm won't do it for you.

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

> **⚠ Substitute EVERY shared value, not just the secret-shaped ones
> (2026-08-16).** An earlier draft of this section argued the two ASNs could be
> literals because a private-range AS number reveals nothing. That reasoning is
> sound and *answers the wrong question*: blinding asks "does this need hiding?",
> but the reason these are variables is **single-sourcing**. Every one of them is
> defined once in Ansible and consumed by both sides of the peering, so a literal
> in Git is a second copy that can drift.
>
> | Value | In Git as | Defined in |
> |---|---|---|
> | `peerIP` | `${bgp_peer_ip}` | `dmz_network.gateway` (vaulted) |
> | LB range | `${lb_range}` | `lb_range_base` + cluster `index` |
> | `BGPPeer.spec.asNumber` | `${bgp_peer_asn}` | `bgp_peer_asn` |
> | `BGPConfiguration.spec.asNumber` | `${cluster_asn}` | `bgp_asn_base` + cluster `index` |
>
> The bottom two are **derived**, which is what makes a literal actively
> dangerous rather than merely redundant: change a cluster's `index`, or add a
> second cluster, and Git still says `64601` while Ansible and pfSense have moved
> on. The session simply never establishes and nothing in the diff looks wrong.
> This matches the root `CLAUDE.md` tier table, which lists "BGP peer IP/**ASN**,
> LB range" under post-build substitution.
>
> ⚠ **The LB range appears in TWO places on the Calico side**, and they are
> different mechanisms: the pool LB IPs are *allocated* from (Calico 3.32's
> LoadBalancer IPAM — the feature with the broken RBAC grant in
> [#12890](https://github.com/projectcalico/calico/issues/12890), whose
> workaround must land **with** the BGP CRs) and
> `BGPConfiguration.spec.serviceLoadBalancerIPs`, which controls *advertisement*.
> Confirm the exact 3.32 CR shape before writing them — that release also moved
> the CRDs out of the chart.

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
- **`${...}` in a YAML comment is safe — with one exception.** kustomize strips
  comments when it re-emits resources, so prose like "an undefined `${var}`
  reconciles as empty" never reaches the cluster. **But a file pulled in by
  `configMapGenerator` is embedded as *data*, comments and all** — so a `${...}`
  written in a comment inside `values.yaml` WOULD survive, get substituted, and
  under `StrictPostBuildSubstitutions` fail the whole reconcile. Verified with
  `kubectl kustomize`; re-check if that pattern spreads to other controllers.
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
`BGPPeer`, and `BGPFilter` are **Calico CRs**, so they're plain manifests — they
can't go through `valuesFrom`.

> **⚠ They live in `infrastructure/calico-bgp/`, NOT `infrastructure/calico/`
> (2026-08-16).** Forced by kustomize, and verified rather than assumed:
> `calico/kustomization.yaml` sets `namespace: tigera-operator` for the
> HelmRelease and generated ConfigMap, and **the namespace transformer stamps a
> namespace on every resource it can't prove is cluster-scoped.** It ships
> schemas for core kinds — hence the ClusterRole/ClusterRoleBinding come out
> clean — but not for CRDs, so `BGPConfiguration`/`BGPPeer`/`BGPFilter`/`IPPool`
> all emerged carrying `namespace: tigera-operator` despite being cluster-scoped.
>
> The obvious fix fails too: a JSON6902 `op: remove` on `/metadata/namespace`
> errors with *"Unable to remove nonexistent key"*, because **patches run before
> the namespace transformer**. A sibling directory outside the transformer's
> scope is the robust answer, and the split says something true —
> `calico/` installs Calico, `calico-bgp/` configures it.
>
> **Generalizes:** any future cluster-scoped CR added under a kustomization with
> a `namespace:` will hit this. Check with `kubectl kustomize` rather than
> trusting that the applier ignores a stray namespace.

⚠ **These are `projectcalico.org/v3` resources** — served by the aggregated
**calico-apiserver**, not by the CRDs in `crds/`. So they need Calico *running*,
not merely its CRDs Established. Ansible primes them before Flux ever sees them,
so Flux's first reconcile is an adoption; if that ordering ever changes,
`calico-bgp` needs its own Flux Kustomization with `dependsOn` on the
HelmRelease.

- `values.yaml` moves to `bgp: Enabled` + **no encapsulation** (from
  `VXLANCrossSubnet`). Calico's VXLAN path uses no BGP at all, so this makes BGP
  load-bearing for pod routing — see `ansible/CLAUDE.md` §6 step 5 and §7 item 13.
- All nodes share one subnet, so the default **node-to-node mesh** distributes
  pod CIDRs with no `BGPPeer`. The pfSense peer is for LB advertisement only.
- `BGPFilter` (via `BGPPeer.spec.filters`) exports **only the LB range** and
  explicitly rejects the rest, so pfSense never learns the pod CIDR. Filters
  attach to `BGPPeer`s and the mesh isn't one, so the dataplane is unaffected.
  - **⚠ The catch-all Reject rules are load-bearing.** Calico: *"If an address
    does not match any explicit BGP filter rule, the default action is
    `Accept`."* A filter that only *accepts* the LB range therefore still exports
    the pod CIDR. `In 0.0.0.0/0 -> Reject` last (rules are first-match-wins) is
    what makes it a whitelist rather than a suggestion.
  - `matchOperator: In` covers **both** advertisement shapes — the whole block
    under `externalTrafficPolicy: Cluster` and a /32 per Service under `Local`.
    Same reasoning as `le 32` on the pfSense prefix list, other end of the wire.
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
- **🔁 `assignIPs` is left at `AllServices` (decided 2026-08-16) — REVISIT IF A
  SECOND LoadBalancer IPAM PROVIDER IS EVER ADDED.** Calico assigns an address to
  every LoadBalancer Service, which is right *only* because it's the sole LB IPAM
  here. Adding MetalLB, a cloud controller, kube-vip, or anything else that hands
  out LoadBalancer addresses makes this a conflict — and the trigger is **adding
  the provider**, not waiting for a symptom.
  - The conflict is narrower than it looks: Calico **skips** any Service whose
    `spec.loadBalancerClass` is something other than `calico`, regardless of
    `assignIPs`. A provider that claims its own class is already safe. The real
    collision is a provider that watches *unclassed* Services — then both assign
    and last-writer-wins, showing up as an EXTERNAL-IP that changes by itself or
    an address from the wrong pool that pfSense has no route to.
  - Fix and its footgun (setting `RequestedServicesOnly` without adding
    `loadBalancerClass: calico` turns every existing LB Service `pending` at
    once), plus an unverified note about operator ownership of
    `KubeControllersConfiguration`: see the header in
    `infrastructure/calico-bgp/ippool-loadbalancer.yaml`.

## Conventions

- Chart versions are pinned literally (Renovate bumps them); never `*`/floating.
- HelmRelease `releaseName` + namespace are explicit and stable — they're the
  adoption key when a release is Ansible-primed first.
- A controller that needs shared Helm values across Ansible + Flux uses the
  `values.yaml` + `configMapGenerator(disableNameSuffixHash)` + `valuesFrom`
  pattern above, so there's exactly one values source.

## Not here yet

- ~~**Flux itself**~~ — **LIVE as of 2026-08-29** via
  `ansible/playbooks/flux-bootstrap.yml`. The `deployment/` entrypoints are no
  longer inert: all four Kustomizations reconcile, and Flux adopted the
  Ansible-primed Calico release (helm rev 1→2, "Helm upgrade succeeded",
  `Installation.spec` unchanged, zero pod restarts) plus the BGP CRs, which now
  carry `kustomize.toolkit.fluxcd.io/name=infrastructure`. **The
  "Ansible primes, Flux adopts" pattern below is no longer a plan — it is
  verified.** Notes that matter from this side:
  - **The operator is Ansible-owned and is NOT primed-for-adoption.** Nothing in
    `gitops/` manages the flux-operator, so unlike Calico there is no second
    writer and no HelmRelease here for it. Self-management (a HelmRelease for the
    operator, reconciled by the Flux that operator installed) is a real pattern
    and a real footgun — an in-flight upgrade can delete the controller
    performing it. Adopt it deliberately or not at all; don't drift into it.
  - **The `FluxInstance` is applied in two passes, the first WITHOUT
    `spec.sync`**, so the `StrictPostBuildSubstitutions` gate is verified present
    before Flux is allowed to reconcile anything from this tree. Without the
    gate, the first `infrastructure` reconcile applies `peerIP: ""` over a
    working `BGPPeer` and reports Healthy. See `ansible/CLAUDE.md`.
  - **No pull secret** — the repo is public. If it ever goes private that
    changes, and `spec.sync.pullSecret` + a BWS secret are the fix.
  - Still to seed if SOPS is ever needed: `sops-age`. `cluster-topology` is
    already seeded by `bootstrap-cluster.yml`.
- **✅ DONE (step 3, branch `step3-oci-flux-source`, 2026-08-30; not yet merged
  or run) — source is OCI, not Git.** The `deployment/homelab/` entrypoints now
  point at `OCIRepository/flux-system` (`source.yaml`), reconciled by a committed
  root `flux-system` Kustomization (`sync.yaml`); the FluxInstance is sync-less.
  Flux pulls the versioned, cosign-verified OCI **artifact** rather than the Git
  branch. History of what this needed:
  - **A GitHub Actions workflow** that builds the gitops tree into an OCI
    artifact and pushes it to a registry (e.g. GHCR) — on merge to `main`
    and/or tag (`flux push artifact oci://…` or the equivalent action).
  - **⚠ TODO at that milestone — render the Calico CRDs at build time and stop
    vendoring them.** `crds/calico/crds.yaml` is **3.0 MB of generated output
    committed to Git**, and every `calico_version` bump adds another full copy
    to history forever. That was accepted knowingly (2026-08-02) because it's
    the only option that gives Flux the ordering guarantee *today* — see "The
    CRD tier". Once the artifact is built by CI, the workflow can run
    `helm template calico-crds crd.projectcalico.org.v1 --version <pin>` into
    the artifact instead, so the 3 MB exists in the OCI layer and never in Git.
    **Two constraints that must survive the move:** (a) the version must come
    from the *same* pin as `calico_version`/`helmrelease.yaml`, or the three
    drift silently; (b) `bootstrap-cluster.yml` primes from the vendored file —
    if Git stops carrying it, Ansible needs its own render at that same pin, and
    the "one source, primed twice" invariant has to be re-established some other
    way (a CI-published artifact both sides consume, most likely). Don't delete
    the vendored file until that second half is actually solved.
  - **✅ DECIDED 2026-08-29 — cosign, keyless via the GHA OIDC identity, verified
    by `OCIRepository.spec.verify` + `matchOIDCIdentity`.** Not a preference:
    `spec.verify.provider` is an enum of exactly **`cosign`/`notation`**, so
    **GitHub artifact attestations cannot gate reconciliation at all**. They
    compose fine as extra SLSA provenance, but they are not admission control —
    do not swap one for the other. Keyless over a key because a long-lived key
    is another BWS secret to store/rotate/leak, while issuer+subject matching
    asserts the stronger *"signed by this workflow, in this repo"*.
  - **✅ DECIDED 2026-08-29 — the FluxInstance stays SYNC-LESS; Ansible seeds the
    `OCIRepository` + root `Kustomization`.** ⚠ `spec.sync` accepts
    `OCIRepository` as a kind but has **NO `verify` field** (8 fields only —
    checked against the live CRD *and* the operator's `main`, where the `Sync`
    struct is unchanged and no issue tracks adding it). Wiring OCI through
    `spec.sync` would therefore look finished while the signature gate was
    silently absent. Patching the generated source doesn't help either: the
    operator holds it in `status.inventory` and reconciles the edit away.
  - **The root source is committed INSIDE the path it reconciles**, so Flux
    adopts it and then drift-corrects it — the same "Ansible primes, Flux adopts"
    handoff as Calico. ⚠ **Self-management is a LAYOUT property, not an automatic
    one** (`flux bootstrap` gets it by writing `gotk-sync.yaml` into its own
    `TargetPath`); lay it out any other way and nothing heals the root.
  - ⚠ **Two sharp edges of that self-management:** a future signed artifact that
    drops `verify` would **disable its own verification** (not a hole — it must
    be signed by our identity to apply — but treat that file like a CI secret,
    not routine review); and a bad committed root is self-inflicted lockout,
    recovered only by re-running `flux-bootstrap.yml`, which is why that play
    must stay idempotent rather than one-shot.
  - Full rationale + everything rejected: **Appendix A, "GitOps delivery"**.
  - **✅ DONE + VERIFIED 2026-08-29 — step 4 is live.**
    `.github/workflows/gitops-artifact.yml` publishes
    `ghcr.io/nighlabs/homelab-infra/gitops`. Verified independently, not just
    "the job went green": `cosign verify` against
    `--certificate-identity-regexp='^https://github.com/nighlabs/homelab-infra/'`
    + the GitHub OIDC issuer passes (claims validated, transparency-log entry
    confirmed, cert chained to a trusted CA), and the pulled layer contains all
    21 manifests and **zero markdown**. The package is **public** (anonymous
    pull works), so **no image pull secret is needed** — one less bootstrap-tier
    secret than expected.
  - ⚠⚠ **THE ARTIFACT ROOT IS `gitops/` ITSELF — THE PREFIX IS GONE.** Paths
    inside it are `deployment/homelab/…`, `infrastructure/…`, `crds/…`. **✅ Done
    in step 3:** every tier path lost its `./gitops` prefix (`./crds`,
    `./infrastructure`, `./apps`) and the root sync path is `./deployment/homelab`
    (`sync.yaml`). The failure this prevents — source Ready, Kustomization failing
    "kustomization path not found" — is the one already burned into this repo's
    history, so keep the prefix off any new tier added under the OCI source.
  - ⚠ `--reproducible` stabilises the LAYER digest, not the manifest digest —
    `org.opencontainers.image.revision` embeds the commit SHA, so every build
    mints a new manifest digest and therefore a new OCIRepository revision.
    That is why the workflow negates `gitops/**/*.md` in its trigger `paths`
    rather than relying on the ignore list alone.
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
