# CLAUDE.md — gitops/ (Flux-managed cluster contents)

Flux-managed cluster state. `ansible/playbooks/bootstrap-cluster.yml` primes
Calico from these files; `ansible/playbooks/flux-bootstrap.yml` then installs
the flux-operator and seeds the source + root Kustomization committed here, and
from that point **changes arrive through the signed artifact, not through
Ansible**. Repo-wide guardrails (no MetalLB, no second Ceph, no CGNAT CIDRs, no
DHCP, nothing secret in Git) apply here too — root `CLAUDE.md`. Why anything is
the way it is: `../docs/decisions/` (ADR-NNNN). What's been verified when:
`../docs/worklog.md`.

## How this tree reaches the cluster

- **CI builds this directory into an OCI artifact and cosign-signs it keyless**
  (`.github/workflows/gitops-artifact.yml` →
  `oci://ghcr.io/nighlabs/homelab-infra/gitops`). Flux reconciles from an
  `OCIRepository` whose `spec.verify` + `matchOIDCIdentity` refuse anything not
  signed by this repo's workflow identity (ADR-0028). **Pushing to `main` does
  not reach the cluster on its own** — merge → CI signs → Flux pulls.
- **The artifact root is `gitops/` itself.** Every path Flux is given is
  artifact-relative: `./deployment/testnode`, `./crds`, `./infrastructure`,
  `./apps` — **no `./gitops` prefix**. Get this wrong and the source goes Ready
  while the Kustomization fails "path not found".
- The `latest` tag is mutable by design and is moved only *after* the digest
  is signed. To freeze on a known-good artifact, set `ref.digest` in
  `deployment/testnode/source.yaml`. `--reproducible` stabilises the layer
  digest, not the manifest digest (the revision label embeds the commit SHA),
  which is why the workflow's trigger negates `gitops/**/*.md`.
- The package is **public**, so there is no pull secret. If it ever goes
  private: a 1Password-sourced Secret + `spec.secretRef` on the `OCIRepository`.

## Layout (four tiers)

```
deployment/<cluster>/     # Flux entrypoints — the ONE path Flux is told about
  source.yaml               #   OCIRepository flux-system (signed, verified)
  sync.yaml                 #   root Kustomization flux-system -> ./deployment/<cluster>
  crds.yaml                 #   -> ./crds            (prune: false, wait: true, no postBuild)
  infrastructure.yaml       #   -> ./infrastructure  (dependsOn crds, postBuild cluster-topology)
  infrastructure-config.yaml#   -> ./infrastructure-config (dependsOn infrastructure, postBuild)
  apps.yaml                 #   -> ./apps            (dependsOn infrastructure-config, postBuild)
  kustomization.yaml        #   lists all six — adding a tier is a visible diff
crds/                     # CRDs that must be Established BEFORE controllers
  calico/                   #   vendored, server-side applied (v3.32 chart carries no CRDs; 3 exceed the CSA limit)
  gateway-api/              #   vendored standard-channel bundle (belongs to NO chart; httproutes exceeds the CSA limit)
  ceph-csi/                 #   vendored (operatorconfig 536 KB + driver 505 KB, each ~2x the CSA limit; chart has NO toggle)
infrastructure/           # controllers, in dependency order:
  calico/                   #   INSTALLS Calico (operator chart + shared values.yaml + endpoint ConfigMap)
  calico-bgp/               #   CONFIGURES it: BGP CRs, LB IPAM pool
  nginx-gateway-fabric/     #   Gateway API impl (ADR-0013): NGF chart, shared Gateway, https redirect
  cert-manager/             #   controller only — its CRs live a tier down
  ceph-csi-operator/        #   VENDORED manifests, not a HelmRelease (ADR-0031)
  external-secrets/         #   ESO chart (1Password SDK provider; chart installs its CRDs, ADR-0039)
infrastructure-config/    # CRs CONSUMED BY those controllers (CRDs arrive with the chart,
  cert-manager/           #   so same-pass apply fails): ClusterIssuers + wildcard Certificate
  ceph-csi/               #   CephConnection, ClientProfile, 2 Drivers, 2 StorageClasses
  external-secrets/       #   2 ClusterSecretStores + the platform ExternalSecrets (ADR-0038)
apps/                     # workloads only (empty until the infra layer is up)
```

- **`deployment/<cluster>/` is self-managing by layout.** `source.yaml` and
  `sync.yaml` live *inside* the path the root reconciles, so Flux adopts them
  after Ansible seeds them and drift-corrects them thereafter — the same
  mechanism `flux bootstrap` uses with `gotk-sync.yaml`. Move them elsewhere
  and nothing heals the root. ⚠ Two sharp edges: a signed artifact that drops
  `verify` disables its own verification (not a hole — it must still be signed
  by our identity — but review `source.yaml` like a CI secret); and a bad
  committed root is self-inflicted lockout, recovered by re-running
  `flux-bootstrap.yml` (which is why that play stays idempotent).
- **The directory name IS the cluster key** from `ansible/inventory/nodes.yml`
  (`testnode`) — `flux_sync_path` derives from it. Rename both or neither.
- **`infrastructure/` vs `apps/`:** controllers (CNI, ingress, CSI, secrets) in
  `infrastructure/`; only workloads in `apps/`. `apps` `dependsOn`
  `infrastructure`, so nothing reconciles before the controllers are Ready.
  Ordering *within* a tier is Flux `dependsOn` between HelmReleases /
  Kustomizations.
- ⚠ **`wait: true` only gates on what kstatus can SEE.** A resource with no
  `status` is treated as Current immediately, so a tier full of statusless CRs
  goes Ready at once. `infrastructure-config` therefore does **not** gate on
  ceph-csi's driver pods (`Driver`/`ClientProfile`/`CephConnection` all expose
  an empty status) — it went Ready while they were still `ContainerCreating`.
  Consequence: **`apps` does not gate on storage being functional.** Contrast
  cert-manager, whose `Certificate` carries a real Ready condition, which is why
  that tier's gate genuinely means "issuance worked". Don't generalise from it:
  before relying on `wait: true`, check the resource actually reports status.
- **`postBuild.substituteFrom` is on `infrastructure`, `infrastructure-config`
  and `apps`, deliberately not on `crds` or the root.** kustomize-controller only runs substitution when
  `spec.postBuild` is set; leaving it off `crds` keeps ~5 MB of generated CRD
  text from being scanned and, under the strict gate, from hard-failing the
  tier everything depends on. `apps` has the block *before* it has any
  placeholder, so the first one added is covered by the gate.

## "Ansible primes, Flux adopts" (ADR-0016)

Flux's own pods need a CNI, but the CNI is Flux-managed. Resolution: Ansible
installs Calico **once** from the *same committed definition*, and Flux's first
reconcile is an adoption with no diff — not a k3s autoload manifest (the
AddonManager would fight Flux, and deleting the manifest prunes Calico).

- **One `values.yaml`, primed identically twice.** `bootstrap-cluster.yml` feeds
  `infrastructure/calico/values.yaml` straight to `helm`; the
  `configMapGenerator` turns the *same file* into the `calico-values` ConfigMap
  that `helmrelease.yaml` reads via `valuesFrom`. `disableNameSuffixHash: true`
  is **required** — a hashed name never resolves the fixed `valuesFrom`.
- **Adoption keys:** release name + namespace (`tigera-operator` /
  `tigera-operator`) and chart `version:` must match what Ansible installed.
  `calico_version` in `ansible/inventory/group_vars/all/vars.yml` and
  `helmrelease.yaml`'s `version:` move in lockstep (both Renovate-tracked).
- **The same holds for the CRDs, the BGP CRs and the endpoint ConfigMap** — Ansible applies the committed files, Flux adopts. When
  Ansible applies a manifest carrying `${var}`, it uses
  `flux build kustomization --strict-substitute` with cluster access — never
  `kustomize build` (which emits the literal `${var}`, a valid string, and
  applies it cleanly), and never `--dry-run` (which **skips** Secret/ConfigMap
  substitutions).
- **Keep the dual-applied set small.** It is exactly the items above plus the
  Flux root. Everything else is Flux-only.
- A healthy adoption is *boring*: HelmRelease Ready, no Calico pod restarts,
  BGP session stays up. If the HelmRelease sticks not-Ready, diff `values.yaml`
  against `helm -n tigera-operator get values tigera-operator`.
- ADR-0029 (Proposed) would replace the Helm half with a manifest install and
  dissolve the release-identity matching entirely.

## The CRD tier (ADR-0020)

Calico v3.32 removed its CRDs from the tigera-operator chart. **A second
HelmRelease with `dependsOn` does NOT work**: three of the 32 CRDs exceed the
262144-byte client-side apply limit and need server-side apply, which
helm-controller can't do. kustomize-controller applies server-side by default,
hence `crds/calico/crds.yaml` — 3 MB of generated output, **regenerated (never
hand-edited) on every `calico_version` bump, in the same commit as `vars.yml`
+ `helmrelease.yaml`**, via the command in its header.

- **`prune: false` on the `crds` Kustomization is deliberate.** Pruning a CRD
  garbage-collects every CR of that kind — for Calico, the entire network
  config. Removing a CRD is a manual act, never a reconcile.
- **`wait: true`** so `infrastructure`'s `dependsOn` gates on *Established*.
- **Don't route other controllers' CRDs here** just because they have some.
  ⚠ **Size alone does NOT qualify** (ADR-0039): the 262144-byte limit is the
  `last-applied-configuration` annotation that client-side `kubectl apply`
  writes, and Helm writes no such annotation. ESO's two ~374 KB CRDs install
  from its chart. A CRD belongs here when no chart can own it for another
  reason: no chart at all (Gateway API), or an Ansible prime sharing the file
  (Calico).
- **Three occupants now, from three unrelated charts** (Calico, Gateway API,
  ceph-csi), so this tier is load-bearing rather than a Calico quirk. ceph-csi
  is the strictest case: its chart has **no `crds.enabled` toggle at all**, so
  the whole operator is installed from vendored manifests (ADR-0031). ⚠ That
  record's size premise is corrected by ADR-0039, so whether ceph-csi moves to
  its chart is open again. To test any CRD, don't measure it:
  `kubectl create --dry-run=server` succeeds where
  `kubectl apply --dry-run=server` fails "Too long".
- **Open follow-on:** render the CRDs at OCI build time so the ~5 MB (Calico
  2.9 MB, Gateway API and ceph-csi 1 MB each) stops living in Git. For
  Calico two constraints must survive: the version must come from the same
  pin as `calico_version`, and `bootstrap-cluster.yml` primes from the
  vendored file — Git can't stop carrying it until Ansible has another source
  at the same pin. **Gateway API and ceph-csi have no Ansible prime**, so
  they are the easy ones and could move first (ADR-0031).

## Topology blinding — `${var}` placeholders (ADR-0021)

The repo is public. Anything environment-revealing is committed as a
placeholder and substituted by Flux from the Ansible-seeded `cluster-topology`
Secret. `cluster-topology` stays Ansible-seeded **permanently**: a Secret
produced by a controller *inside* the `infrastructure` tier can never be a
substitution source *for* that tier (one pass, no intra-tier ordering), and
Calico is primed from the same values before Flux exists. *Cluster-bound ≠
ESO-managed.*

```yaml
# infrastructure/calico-bgp/bgppeer.yaml — committed exactly like this
spec:
  peerIP: ${bgp_peer_ip}
  asNumber: ${bgp_peer_asn}
```

| Placeholder | Defined in |
|---|---|
| `${bgp_peer_ip}` | `dmz_network.gateway` (1Password) |
| `${lb_range}` | `lb_range_base` + cluster `index` |
| `${bgp_peer_asn}` | `bgp_peer_asn` (cleartext constant) |
| `${cluster_asn}` | `bgp_asn_base` + cluster `index` |
| `${k3s_api_ip}` | the primary node's DMZ IP |
| `${cluster_name}` | the cluster key — names its 1Password vaults (ADR-0038) |

- **Substitute EVERY shared value, not just secret-shaped ones.** The ASNs
  reveal nothing, but they're *derived* in Ansible and consumed on both sides
  of the peering; a literal in Git is a second copy that drifts the moment a
  cluster's `index` changes — and the session simply never establishes.
- ⚠ **An undefined `${var}` substitutes to the empty string and reconciles
  SUCCESSFULLY** — `peerIP: ""`, applied, reported healthy. The
  kustomize-controller gate `StrictPostBuildSubstitutions=true` is what turns
  that into a failure; `flux-bootstrap.yml` patches it in and asserts it
  landed. Check locally with `flux envsubst --strict`.
- **`${...}` inside a comment in a `configMapGenerator` file survives** —
  the file is embedded as data, comments and all — and fails the reconcile
  under the strict gate. Elsewhere kustomize strips comments. Don't write
  `${...}` in `values.yaml` comments.
- `$${var}` escapes a literal; `$var` is untouched; substitution into a Secret
  needs `.stringData`; per-resource opt-out is the annotation
  `kustomize.toolkit.fluxcd.io/substitute: disabled`.
- **A YAML LIST can be substituted as one value.** `postBuild` is string-level,
  so `${x}` cannot expand into multiple sequence items — the case this file
  calls "substitution can't go here". It does not always need SOPS: seed ONE
  value that is already a complete JSON array (valid YAML flow sequence) and
  substitute it whole. `CephConnection.spec.monitors: ${ceph_mons_yaml}` does
  this, from `ceph_csi.mons | to_json` in `bootstrap-cluster.yml`, so the mon
  count lives in 1Password rather than being baked into the manifest as
  `${ceph_mon_1..3}`. The `to_json` quoting is load-bearing — an unquoted
  `host:port` is not a valid scalar in flow context.
- **Keep substituted resources out of kustomize `Components`** —
  [kustomize-controller#1506](https://github.com/fluxcd/kustomize-controller/issues/1506).
  Plain `resources:` entries only.
- **SOPS/age is the fallback, not the default** — only where substitution
  can't go (whole blocks/lists, kustomize-*build*-time values). If ever
  needed: the age key arrives as Secret `sops-age` in `flux-system`, key file
  `age.agekey`, Ansible-seeded from 1Password; `apiVersion`/`kind`/`metadata` can
  never be encrypted.

## Calico BGP — CRs, not Helm values (ADR-0018, ADR-0023)

`BGPConfiguration`, `BGPPeer`, `BGPFilter` and the LoadBalancer `IPPool` are
Calico CRs, so they're plain manifests — they can't go through `valuesFrom`.

- **They live in `infrastructure/calico-bgp/`, NOT `calico/`.** Forced by
  kustomize and verified: `calico/kustomization.yaml` sets
  `namespace: tigera-operator`, and the namespace transformer stamps a
  namespace on every resource it can't prove is cluster-scoped — it has schemas
  for core kinds but not CRDs, so the BGP CRs emerged with
  `namespace: tigera-operator`. A JSON6902 `remove` fails too (patches run
  before the transformer). **Generalises:** any cluster-scoped CR under a
  kustomization with a `namespace:` hits this; check with `kubectl kustomize`.
- ⚠ **These are `projectcalico.org/v3` resources served by the aggregated
  calico-apiserver**, not by the CRDs in `crds/`. They need Calico *running*.
  Ansible primes them before Flux sees them; if that ordering ever changes,
  `calico-bgp` needs its own Flux Kustomization with `dependsOn` the HelmRelease.
- `values.yaml`: `bgp: Enabled`, `encapsulation: None`, `linuxDataplane: BPF`.
  Calico's VXLAN path uses no BGP at all, so BGP is **load-bearing for pod
  routing**. All nodes share one subnet, so the default node-to-node mesh
  distributes pod CIDRs with no `BGPPeer`; the pfSense peer is for LB
  advertisement only.
- **`BGPFilter` exports only the LB range and ends in `In 0.0.0.0/0 -> Reject`.**
  Calico's default for an unmatched route is **Accept**, so a filter that only
  *accepts* the LB range still exports the pod CIDR. `matchOperator: In` covers
  both the `/24` block (`externalTrafficPolicy: Cluster`) and a `/32` per
  Service (`Local`) — same reasoning as `le 32` on the pfSense prefix list.
  pfSense's inbound prefix list is the **independent** second control; if the
  pod CIDR ever shows in `vtysh -c 'show ip bgp'`, both failed.
- **🔁 `assignIPs: AllServices` — revisit if a second LoadBalancer IPAM
  provider is ever added** (MetalLB, kube-vip, a cloud controller). Calico
  skips Services whose `loadBalancerClass` isn't `calico`, so a provider that
  claims its own class is safe; one that watches unclassed Services collides
  (last writer wins — an EXTERNAL-IP that changes by itself). The trigger is
  adding the provider, not a symptom. Footgun: `RequestedServicesOnly` without
  adding `loadBalancerClass: calico` turns every LB Service `pending` at once.
  Details in the header of `calico-bgp/ippool-loadbalancer.yaml`.
- **`kubernetes-services-endpoint` ConfigMap** (`calico/`): eBPF mode needs the
  API server address without a ClusterIP. **Real IP, never `localhost`**
  (calico#9141 — kube-controllers dies on `[::1]:6443` while everything else
  comes up). It's topology, so `${k3s_api_ip}`, Ansible-primed.

## Conventions

- Chart versions are pinned literally (Renovate bumps them); never floating.
- HelmRelease `releaseName` + namespace are explicit and stable — they're the
  adoption key when a release is Ansible-primed first.
- A controller that needs shared Helm values across Ansible + Flux uses the
  `values.yaml` + `configMapGenerator(disableNameSuffixHash)` + `valuesFrom`
  pattern, so there's exactly one values source.
- A cluster-scoped CR goes in a directory *without* a `namespace:` transformer.
- Nothing environment-revealing is ever a literal: `${var}` + `cluster-topology`.

## ESO + 1Password (ADR-0034, ADR-0038)

- **No in-cluster secrets server.** The 1Password SDK provider calls the vendor
  API directly: no Deployment, Service, `Certificate` or image digest to
  maintain. ESO's webhook certs come from its bundled `cert-controller`, never
  cert-manager. Its earliest position is immediately after Calico: CRDs,
  pod networking, egress to the vendor API (pod→node NAT, **not** BGP) and its
  token Secret.
- **Two ClusterSecretStores, one per vault** (a store names exactly one):
  `onepassword-platform` → `homelab-platform-${cluster_name}`, restricted by
  `conditions.namespaces` to `cert-manager` and `ceph-csi` (an app namespace
  cannot pull the Cloudflare token), and `onepassword-apps` →
  `homelab-apps-${cluster_name}`, unrestricted. Both authenticate with the
  Ansible-seeded `external-secrets/onepassword-service-account` (key `token`).
  ⚠ Add a namespace to the platform store only for a platform controller.
- **Platform Secrets: Ansible seeds, ESO keeps current.** The Cloudflare token
  and both cephx Secrets are **created** by `bootstrap-cluster.yml` and then
  synced by ExternalSecrets with **`creationPolicy: Merge`**, from the same
  1Password fields. Merge never creates, never deletes, takes no
  ownerReference, and writes only the listed keys (the cephx `userID` stays
  Ansible's). ESO does mark a merged Secret: it adds the label
  `reconcile.external-secrets.io/managed=true` and a `data-hash` annotation.
  Ansible's re-seed leaves both alone, since it never set them. So a rebuild works before ESO exists, and deleting an
  ExternalSecret can't take cert-manager's token with it. ⚠ Don't switch them
  to `Owner`/`CreateOrMerge`: ESO would then create a Secret alone, without
  `userID`.
- **Rotation:** edit the field in 1Password, then
  `kubectl -n <ns> annotate externalsecret <name> force-sync=$(date +%s) --overwrite`.
  The Secret updates within seconds (verified with the Cloudflare token,
  worklog 2026-10-05). ⚠ To prove a rotated DNS-01 token *works*, don't
  re-issue an existing certificate: Let's Encrypt reuses a still-valid
  authorization and no challenge runs (the order shows
  `initialState: valid`). Issue a throwaway Certificate for a never-validated
  hostname instead, then delete it.
  The platform store has no provider `cache:` on purpose, since a cache would
  answer that force-sync from memory.
- ⚠ **Rate limit is the design constraint**: 1,000 requests/24h per *account*
  on Individual/Families, shared by every service account. Store validation
  is free in steady state (the client is cached per store `resourceVersion`
  and `Validate()` makes no call). Observed live: 6 `VaultsList` once at
  controller start for the two stores, then none across requeues. The cost is
  one `Resolve` per data entry per refresh. **Set `refreshInterval` deliberately on every ExternalSecret**;
  the platform ones use `24h` (3 calls/day). Budget roughly
  `Σ entries × 24h/interval` before adding a batch.
- ⚠ **Retry behaviour: there is nothing in ESO to tune** (read from the
  v2.11.0 source). It exposes no backoff flags, and `refreshInterval` and
  `--store-requeue-interval` govern only the *success* path. A failed
  reconcile returns an error to controller-runtime's default queue: 5 ms,
  doubling, **capped at 1000 s (~16.7 min), retried forever**. That is
  ~18 calls in the first ~20 min, then **~86/day per failing object**, until
  it succeeds or is deleted. How that plays out by failure mode:

  | Failure | What retries | Cost |
  |---|---|---|
  | Token bad / expired / revoked | **Only the 2 stores.** The flood gate (`--enable-flood-gate`, default on) stops ExternalSecrets calling the provider while their store is NotReady | ~170/day: survivable, no lockout |
  | Token fine, one item/field missing or renamed | **That ExternalSecret.** Its store stays Ready, so the flood gate doesn't apply | ~86/day per broken ExternalSecret, which is the real amplifier: 10 broken refs ≈ 860/day |
  | Account already rate-limited | Every ExternalSecret that tries to sync; the stores stay Ready, because their cached client makes no call | N × 86/day of rejected calls, extending the lockout if rejected calls count |

  So the controls that matter are outside ESO. Keep the count of
  ExternalSecrets low, and give each one a deliberate interval. **Verify
  every new remote ref before merging**: a typo'd field costs ~86 calls/day
  until fixed. Check the token before seeding it: `bootstrap-cluster.yml` runs
  `op vault list` as the token and refuses unless it sees exactly the cluster's
  two vaults. When rotating the token, run the play, never a raw
  `kubectl create secret`.
- 🛑 **Emergency brake**: `kubectl -n external-secrets scale deploy external-secrets --replicas=0`.
  This stops every 1Password call immediately. Every synced Secret persists
  (Merge never deletes; materialised Secrets outlive their source), so
  workloads are unaffected; only freshness pauses. helm-controller doesn't
  revert the replica count (no drift detection configured), but the next
  chart *upgrade* will, so suspend the HelmRelease too if the brake must
  hold: `flux -n external-secrets suspend hr external-secrets`. Release with
  `resume` and scale back to 1.
- **Watch the spend:** the controller exports
  `externalsecret_provider_api_calls_count{provider="1Password/SDK"}` (by
  `call`: `Resolve`, `VaultsList`, …, and `status`) on `:8080`. ⚠ It counts
  ESO's calls, not the SDK's own sign-in when a client is built, so treat it
  as a floor, not the bill; 1Password's usage report is the bill. Nothing scrapes it until the OTel
  milestone. Until then, `kubectl -n external-secrets port-forward deploy/external-secrets 8080`
  and curl `/metrics`. An alert on its daily rate is owed at that milestone.
- ⚠ **`infrastructure-config`'s `wait: true` now gates `apps` on 1Password
  being reachable** (the ExternalSecrets and stores report real Ready), even
  though the seeded Secrets keep working. Accepted for now; see ADR-0040 (Open).
- **CRDs come from the chart** (`installCRDs: true`) with
  `helm.sh/resource-policy: keep`, so an uninstall never deletes them and
  garbage-collects every ExternalSecret (ADR-0039).
- Per-app isolation *inside* a cluster (namespace-scoped stores, or per-app
  conditions on `onepassword-apps`) is deferred until `apps/` has more than
  one tenant.

## Next

Everything downstream of secrets, in dependency order (the `NginxProxy`
RewriteClientIP config still comes with Cloudflare Tunnel — ADR-0013):
Postgres + Redis → LiteLLM → Qdrant → RAG → Open WebUI → OTel. Design:
`../docs/architecture.md` §3.8, §4.5–4.9, §7. App secrets go to
`homelab-apps-<cluster>` via `onepassword-apps`, each ExternalSecret with a
deliberate `refreshInterval`.
