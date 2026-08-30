# ADR-0021: Topology values are `${var}` placeholders in Git, substituted by Flux from the Ansible-seeded `cluster-topology` Secret; SOPS only where substitution can't go

- **Date:** 2026-08-02 (decided) · 2026-08-16 (extended: substitute *every* shared value) · 2026-08-29/30 (verified live)
- **Status:** Accepted
- **Supersedes / related:** [ADR-0009](0009-secrets-aescbc-and-eso-bitwarden.md) (the SOPS alternatives already rejected for *credentials*), [ADR-0027](0027-control-node-secrets-bws-runtime.md) (why `cluster-topology` stays Ansible-seeded), [ADR-0018](0018-calico-bgp-replaces-metallb.md) (the BGP CRs that forced this), [ADR-0028](0028-gitops-delivery-signed-oci-syncless-fluxinstance.md) (the strict gate is applied via the FluxInstance). Code: `gitops/deployment/homelab/infrastructure.yaml` / `apps.yaml` (`postBuild`), `ansible/playbooks/bootstrap-cluster.yml` (seeds the Secret), `ansible/playbooks/tasks/flux-bootstrap-cluster.yml` (asserts the gate).

## Context

This repo (and its GHCR package) is **public**. Real subnets, VLAN tags, peer
addresses and ranges are environment-identifying and stay out of Git — the
root `CLAUDE.md` calls this *topology blinding*, a tier distinct from
credentials. Calico dodged the question with
`nodeAddressAutodetectionV4.kubernetes: NodeInternalIP` (k3s advertises the
DMZ IP, so nothing in Git names the subnet), but the BGP CRs can't: **a peer IP
and an ASN have no autodetection equivalent.** Something has to put real values
into committed manifests at reconcile time.

## Decision

**Committed manifests carry `${var}` placeholders; Flux's kustomize-controller
substitutes them at reconcile time via `postBuild.substituteFrom` a Secret named
`cluster-topology`, which Ansible seeds at bootstrap from BWS.** Nothing
encrypted is ever committed, values rotate without a commit, and diffs stay
readable.

```yaml
# infrastructure/calico-bgp/bgppeer.yaml — committed exactly like this
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

**Substitute EVERY shared value, not just the secret-shaped ones** (2026-08-16).
An earlier draft argued the two ASNs could be literals because a private-range
AS number reveals nothing. That reasoning is sound and *answers the wrong
question*: blinding asks "does this need hiding?", but the reason these are
variables is **single-sourcing**. Every one of them is defined once in Ansible
and consumed by both sides of the peering, so a literal in Git is a second copy
that can drift.

| Value | In Git as | Defined in |
|---|---|---|
| `peerIP` | `${bgp_peer_ip}` | `dmz_network.gateway` (BWS) |
| LB range | `${lb_range}` | `lb_range_base` + cluster `index` |
| `BGPPeer.spec.asNumber` | `${bgp_peer_asn}` | `bgp_peer_asn` |
| `BGPConfiguration.spec.asNumber` | `${cluster_asn}` | `bgp_asn_base` + cluster `index` |
| `kubernetes-services-endpoint` host | `${k3s_api_ip}` | `dmz_network.subnet_base` + `node_number` |

The bottom two ASN rows are **derived**, which makes a literal actively
dangerous rather than merely redundant: change a cluster's `index`, or add a
second cluster, and Git still says `64601` while Ansible and pfSense have moved
on. The session simply never establishes and nothing in the diff looks wrong.

**The `StrictPostBuildSubstitutions` feature gate is mandatory.** Per the
Flux docs, *"all the undefined variables in the format `${var}` will be
substituted with an empty string unless a default value is provided"* — and
that reconciles **successfully**. A typo gives you `peerIP: ""`, applied and
reported Healthy. kustomize-controller runs with
`--feature-gates=StrictPostBuildSubstitutions=true`, applied through the
FluxInstance's `spec.kustomize.patches` and **asserted present by
`flux-bootstrap.yml` before Flux is allowed to reconcile anything**. `flux
envsubst --strict` checks it locally / in pre-commit.

## Alternatives rejected

- **SOPS/age for topology** — rejected as the default. It commits ciphertext
  (permanent in history, diffs unreadable, a change needs a commit), and
  substitution already covers scalar values. **SOPS/age is the fallback, not
  the default** — reach for it only where substitution can't go: whole
  blocks/lists, or values needed at kustomize-*build* time. If it's ever
  needed: kustomize-controller decrypts non-Secret kinds too (the docs note
  `encrypted_regex` users "may wish to add other fields if you are encrypting
  other types of Objects"), but `apiVersion`/`kind`/`metadata` can never be
  encrypted. The age key would arrive as Secret `sops-age` in `flux-system`,
  key file `age.agekey`, Ansible-seeded from BWS.
- **Literal ASNs in Git** (the 2026-08-02 draft) — rejected 2026-08-16 on the
  single-sourcing argument above.
- **Deliver topology via ESO** — impossible; see Consequences.
- **A private repo instead of blinding** — the repo is public by design and a
  leak was already scrubbed from history once (2026-08-30); blinding is the
  durable fix, not visibility.

## Consequences

- **`cluster-topology` stays Ansible-seeded PERMANENTLY, not just at first
  bootstrap.** The natural assumption is that anything the cluster consumes
  should arrive via ESO. It cannot: ESO needs the Bitwarden SDK Server, which
  needs a cert-manager cert, which needs a Gateway, which needs a
  **LoadBalancer IP** — and the BGP config is what produces that IP. Every
  rebuild needs BGP before ESO can exist. Moving `cluster-topology` to ESO
  "once things are up" would close a real circular dependency. **Cluster-bound
  ≠ ESO-managed.**
- **`infrastructure` and `apps` carry `postBuild`; `crds` and the root
  `flux-system` Kustomization deliberately do not.** `apps` has no placeholders
  yet and gets the block anyway, so the first one added is covered by the gate
  rather than silently substituting to `""`. `crds` must not have it
  ([ADR-0020](0020-crd-tier-vendored-server-side-apply.md)). If a placeholder
  is ever added to the root tier, add the `postBuild` block *with* it.
- **The gate and the `postBuild` block are a pair** — do not remove one
  without the other. The manual check is `kubectl get bgppeer pfsense -o
  jsonpath='{.spec.peerIP}'` returning a real IP, not empty.
- **`${...}` in a YAML comment is safe — with one exception.** kustomize strips
  comments when it re-emits resources, so prose like "an undefined `${var}`
  reconciles as empty" never reaches the cluster. **But a file pulled in by
  `configMapGenerator` is embedded as *data*, comments and all** — so a
  `${...}` written in a comment inside `values.yaml` WOULD survive, get
  substituted, and under the strict gate fail the whole reconcile. Verified
  with `kubectl kustomize`; re-check if that pattern spreads to other
  controllers.
- **Keep substituted resources out of kustomize `Components`** —
  [kustomize-controller#1506](https://github.com/fluxcd/kustomize-controller/issues/1506)
  reports flaky `substituteFrom` there. Components are the same blind spot for
  SOPS decryption, so the rule is just: plain `resources:` entries.
- Escapes: `$${var}` yields a literal; `$var` is untouched; substitution into a
  Secret needs `.stringData`. Disable per-resource with the annotation
  `kustomize.toolkit.fluxcd.io/substitute: disabled`.
- **When Ansible applies one of these manifests it must use `flux build
  kustomization --strict-substitute`, never `kustomize build`** — and note
  `--dry-run` skips Secret/ConfigMap substitutions, so the Secret must exist
  and the build needs cluster access. See
  [ADR-0016](0016-calico-ansible-primes-flux-adopts.md).
- Blinding applies to every committed document too: runbooks and ADRs use
  `${placeholder}` / `x.x.x.N`, never real values. The one committed literal
  network value is the pod CIDR `10.42.0.0/16`, which is non-secret.

## Evidence

Verified on the Flux adoption of 2026-08-29 (`peerIP=${bgp_peer_ip}`,
`clusterASN=64601`, `lb=${lb_range}` all resolved to real values, not `""`) and
on the from-scratch run of 2026-08-30 (`BGPPeer.peerIP` resolved, so
StrictPostBuildSubstitutions worked). See [`../worklog.md`](../worklog.md).
