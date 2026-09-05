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
  artifact-relative: `./deployment/homelab`, `./crds`, `./infrastructure`,
  `./apps` — **no `./gitops` prefix**. Get this wrong and the source goes Ready
  while the Kustomization fails "path not found".
- The `latest` tag is mutable by design and is moved only *after* the digest
  is signed. To freeze on a known-good artifact, set `ref.digest` in
  `deployment/homelab/source.yaml`. `--reproducible` stabilises the layer
  digest, not the manifest digest (the revision label embeds the commit SHA),
  which is why the workflow's trigger negates `gitops/**/*.md`.
- The package is **public**, so there is no pull secret. If it ever goes
  private: a BWS secret + `spec.secretRef` on the `OCIRepository`.

## Layout (four tiers)

```
deployment/<cluster>/     # Flux entrypoints — the ONE path Flux is told about
  source.yaml               #   OCIRepository flux-system (signed, verified)
  sync.yaml                 #   root Kustomization flux-system -> ./deployment/<cluster>
  crds.yaml                 #   -> ./crds            (prune: false, wait: true, no postBuild)
  infrastructure.yaml       #   -> ./infrastructure  (dependsOn crds, postBuild cluster-topology)
  apps.yaml                 #   -> ./apps            (dependsOn infrastructure, postBuild cluster-topology)
  kustomization.yaml        #   lists all five — adding a tier is a visible diff
crds/                     # CRDs that must be Established BEFORE controllers
  calico/                   #   vendored, server-side applied (v3.32 chart carries no CRDs; 3 exceed the CSA limit)
  gateway-api/              #   vendored standard-channel bundle (belongs to NO chart; httproutes exceeds the CSA limit)
infrastructure/           # controllers, in dependency order:
  calico/                   #   INSTALLS Calico (operator chart + shared values.yaml + endpoint ConfigMap)
  calico-bgp/               #   CONFIGURES it: BGP CRs, LB IPAM pool, #12890 RBAC workaround
  nginx-gateway-fabric/     #   Gateway API impl (ADR-0013): NGF chart + the shared Gateway
  kustomization.yaml        #   next: cert-manager -> ceph-csi-operator -> ESO + Bitwarden SDK -> ...
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
  (`homelab`) — `flux_sync_path` derives from it. Rename both or neither.
- **`infrastructure/` vs `apps/`:** controllers (CNI, ingress, CSI, secrets) in
  `infrastructure/`; only workloads in `apps/`. `apps` `dependsOn`
  `infrastructure`, so nothing reconciles before the controllers are Ready.
  Ordering *within* a tier is Flux `dependsOn` between HelmReleases /
  Kustomizations.
- **`postBuild.substituteFrom` is on `infrastructure` and `apps`, deliberately
  not on `crds` or the root.** kustomize-controller only runs substitution when
  `spec.postBuild` is set; leaving it off `crds` keeps 3 MB of generated CRD
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
- **The same holds for the CRDs, the BGP CRs, the #12890 workaround and the
  endpoint ConfigMap** — Ansible applies the committed files, Flux adopts. When
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
  Calico qualifies because of the size limit; a chart whose CRDs fit is fine
  as a HelmRelease.
- **Open follow-on:** render the CRDs at OCI build time so the 3 MB stops
  living in Git. Two constraints must survive: the version must come from the
  same pin as `calico_version`, and `bootstrap-cluster.yml` primes from the
  vendored file — Git can't stop carrying it until Ansible has another source
  at the same pin.

## Topology blinding — `${var}` placeholders (ADR-0021)

The repo is public. Anything environment-revealing is committed as a
placeholder and substituted by Flux from the Ansible-seeded `cluster-topology`
Secret. `cluster-topology` stays Ansible-seeded **permanently** — ESO needs a
LoadBalancer IP that the BGP config produces.

```yaml
# infrastructure/calico-bgp/bgppeer.yaml — committed exactly like this
spec:
  peerIP: ${bgp_peer_ip}
  asNumber: ${bgp_peer_asn}
```

| Placeholder | Defined in |
|---|---|
| `${bgp_peer_ip}` | `dmz_network.gateway` (BWS) |
| `${lb_range}` | `lb_range_base` + cluster `index` |
| `${bgp_peer_asn}` | `bgp_peer_asn` (cleartext constant) |
| `${cluster_asn}` | `bgp_asn_base` + cluster `index` |
| `${k3s_api_ip}` | the primary node's DMZ IP |

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
- **Keep substituted resources out of kustomize `Components`** —
  [kustomize-controller#1506](https://github.com/fluxcd/kustomize-controller/issues/1506).
  Plain `resources:` entries only.
- **SOPS/age is the fallback, not the default** — only where substitution
  can't go (whole blocks/lists, kustomize-*build*-time values). If ever
  needed: the age key arrives as Secret `sops-age` in `flux-system`, key file
  `age.agekey`, Ansible-seeded from BWS; `apiVersion`/`kind`/`metadata` can
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
- **`kube-controllers-ipamconfigs-rbac.yaml`** is the #12890 workaround —
  mandatory on 3.32, Ansible-primed (LB allocation gates Gateway → cert-manager
  → ESO), with a `REMOVE when fixed` header. **Re-check on every Calico bump**:
  remove it and see whether `kubectl auth can-i get ipamconfigs
  --as=system:serviceaccount:calico-system:calico-kube-controllers` still says
  `yes`.
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

## Next

Everything downstream of the Gateway, in dependency order: cert-manager
(DNS-01 wildcard; then add the HTTPS listener + wildcard cert to the Gateway in
`infrastructure/nginx-gateway-fabric/gateway.yaml`, and the `NginxProxy`
RewriteClientIP config when Cloudflare Tunnel arrives — ADR-0013)
→ ceph-csi-operator + StorageClasses → ESO +
Bitwarden SDK Server (its access token is Ansible-seeded from the
`homelab-infra` BWS project; app secrets come from a *separate* project —
ADR-0027; **open, decide at this milestone:** whether ESO adopts the
cluster-destined bootstrap-seeded Secrets such as the cert-manager Cloudflare
token — `docs/decisions/README.md`, "Open questions") → Postgres + Redis → LiteLLM → Qdrant → RAG → Open WebUI → OTel.
Design: `../docs/architecture.md` §3.8, §4.5–4.9, §7.
