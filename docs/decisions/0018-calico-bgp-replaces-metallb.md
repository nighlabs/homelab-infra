# ADR-0018: Calico BGP owns LoadBalancer IP allocation, advertisement, and the pod dataplane; MetalLB is never installed

- **Date:** 2026-07-12 (question raised) · 2026-08-02 (decided) · 2026-08-16 (implemented + verified live on a from-scratch rebuild)
- **Status:** Accepted — **supersedes [ADR-0012](0012-metallb-bgp.md)**
- **Supersedes / related:** [ADR-0010](0010-calico-over-cilium.md) (Calico), [ADR-0019](0019-k3s-1.36-calico-3.32.1-version-pair.md) (the version bump this needed and the #12890 workaround), [ADR-0021](0021-topology-blinding-postbuild-substitution.md) (how the peer IP/ASN reach the cluster), [ADR-0022](0022-pfsense-frr-raw-config-explicit-neighbors.md) / [ADR-0023](0023-rfc8212-real-policy-le32.md) (the pfSense side), [ADR-0024](0024-calico-ebpf-dataplane-no-kube-proxy.md) (independent, but decided alongside), [ADR-0026](0026-per-cluster-derivation-from-index.md) (where the ASN and LB range come from). Runbook: [`../pfsense-frr-bgp-setup.md`](../pfsense-frr-bgp-setup.md). Code: `gitops/infrastructure/calico-bgp/`, `gitops/infrastructure/calico/values.yaml`.

## Context

[ADR-0012](0012-metallb-bgp.md) chose MetalLB in BGP mode to provide
LoadBalancer IPs, peering with pfSense/FRR. On 2026-07-12 an open question was
recorded against it: since we are already standing up a BGP session to FRR for
the LB range *and* Calico is already the CNI, **Calico's own BGP could
advertise LoadBalancer IPs directly** — dropping MetalLB (one fewer tool, one
fewer BGP speaker for FRR to manage), and, if the *dataplane* also moved to
BGP, removing VXLAN encapsulation (less per-packet CPU, no ~50-byte encap tax
shrinking pod-path MTU below 1500). The costs were named at the time: it
couples pod networking to the pfSense BGP fabric (bigger blast radius on the
base CNI layer, the one you least want to churn), and it leans on Calico's
**newer LoadBalancer IPAM** (less battle-tested than MetalLB's allocation).

The recorded plan was **crawl before walk**: ship the simple, decoupled combo
first (Calico VXLAN + `bgp: Disabled`, MetalLB owns LB), then revisit once the
real upstream question — *do we want the pod dataplane on BGP/no-encap at
all?* — was settled. A PoC was to stand Calico BGP up *next to* MetalLB before
committing.

On 2026-08-02 that upstream question was answered **yes**, and the plan was
overturned.

### The fact that drove the sequencing

**Calico's VXLAN implementation uses no BGP at all.** Going no-encap is
therefore not "enable a feature alongside the existing dataplane" — it takes
BGP from *not running* to *the sole mechanism for pod routing between nodes*.
BGP stops being a late-tier LB feature and becomes the dataplane.

### Why churning the CNI was cheapest right then

One node, no workloads, nothing in `apps/`, no PVCs, and Calico still
*Ansible*-managed at helm revision 1 — so re-priming was an Ansible re-run
rather than a fight with Flux. Every week that gets worse. It also meant the
Calico BGP migration had to come **before** the Flux bootstrap, reordering the
milestone plan.

### The two halves have lopsided risk

| | Maturity | What it buys |
|---|---|---|
| Dataplane → BGP, no encap | Core Calico, mature for years | Kills the ~50-byte VXLAN tax + per-packet CPU. The real win. |
| LB IPAM → drop MetalLB | New in 3.30, open bug #12890 | Removes one component. |

The dataplane half is the big, hard-to-reverse change and it is the
**low**-risk one; the MetalLB removal is cosmetic and carries the risk. So:
do the dataplane switch now (free today, expensive after node 2), and take the
LB-IPAM bet knowingly with the #12890 workaround pre-applied
([ADR-0019](0019-k3s-1.36-calico-3.32.1-version-pair.md)).

## Decision

**Calico BGP owns all three: LoadBalancer IP allocation (Calico's LoadBalancer
IPAM), LoadBalancer advertisement to pfSense, and the pod dataplane with no
encapsulation. MetalLB never gets written.**

- `values.yaml` moves to `bgp: Enabled` + `encapsulation: None` (from
  `VXLANCrossSubnet`). `natOutgoing` stays `Enabled` — nothing outside the
  cluster has a route back to the pod CIDR, and the filter below deliberately
  keeps it that way.
- **Three CRs, none of which are Helm values**, live as plain manifests:
  - `BGPConfiguration` — the cluster ASN and `serviceLoadBalancerIPs`
    (advertisement).
  - `BGPPeer` — the pfSense/FRR peer. Global (no `nodeSelector`), so no
    per-node entry.
  - `BGPFilter` — attached via `BGPPeer.spec.filters`. Exports **only the LB
    range** and explicitly `Reject`s everything else. ⚠ **The catch-all
    Reject is load-bearing**: Calico documents that "if an address does not
    match any explicit BGP filter rule, the default action is `Accept`", so a
    filter that only *accepts* the LB range still exports the pod CIDR. `In
    0.0.0.0/0 -> Reject` last (rules are first-match-wins) is what makes it a
    whitelist rather than a suggestion. `matchOperator: In` covers both
    advertisement shapes — the whole block under `externalTrafficPolicy:
    Cluster` and a /32 per Service under `Local` (the same reasoning as `le 32`
    on the pfSense side, [ADR-0023](0023-rfc8212-real-policy-le32.md)).
  - plus an `IPPool` with `allowedUses: [LoadBalancer]` that LB IPs are
    allocated from, and the #12890 RBAC workaround.
- **All k3s nodes share one DMZ subnet** (decided the same day), which is what
  keeps this simple: Calico's **node-to-node mesh is on by default** and
  auto-peers every node with every other in the same L2, so **pod-to-pod
  routing needs no `BGPPeer` at all**. The pfSense peer exists only for LB
  advertisement and external reachability. Filters attach to `BGPPeer`
  resources and the mesh isn't one, so the export filter cannot starve the
  dataplane.
- **The BGP CRs are Ansible-primed and Flux-adopted**, like Calico itself
  ([ADR-0016](0016-calico-ansible-primes-flux-adopts.md)): the dataplane
  depends on them, so they can't wait for Flux. The #12890 workaround is
  primed too, because LB allocation gates the Gateway → cert-manager → ESO
  chain.
- **The CRs live in `infrastructure/calico-bgp/`, NOT `infrastructure/calico/`.**
  Forced by kustomize and verified rather than assumed: `calico/kustomization.yaml`
  sets `namespace: tigera-operator` for the HelmRelease and generated ConfigMap,
  and the namespace transformer stamps a namespace on every resource it can't
  prove is cluster-scoped. It ships schemas for core kinds (so
  ClusterRole/ClusterRoleBinding come out clean) but not for CRDs — so
  `BGPConfiguration`/`BGPPeer`/`BGPFilter`/`IPPool` all emerged carrying
  `namespace: tigera-operator`. The obvious fix fails too: a JSON6902 `op:
  remove` on `/metadata/namespace` errors "Unable to remove nonexistent key",
  because **patches run before the namespace transformer**. A sibling directory
  outside the transformer's scope is the robust answer, and the split says
  something true — `calico/` installs Calico, `calico-bgp/` configures it.
  This generalizes: any future cluster-scoped CR under a kustomization with a
  `namespace:` will hit it; check with `kubectl kustomize`.
- ⚠ These are **`projectcalico.org/v3` resources**, served by the aggregated
  calico-apiserver, not by the CRDs in `crds/`. They need Calico *running*,
  not merely its CRDs Established. Ansible primes them before Flux ever sees
  them; if that ordering ever changes, `calico-bgp` needs its own Flux
  Kustomization with `dependsOn` the HelmRelease.
- **`assignIPs` is left at `AllServices`** (2026-08-16). Calico assigns an
  address to every LoadBalancer Service, which is right *only* because it is
  the sole LB IPAM here.

## Alternatives rejected

- **MetalLB in BGP mode** ([ADR-0012](0012-metallb-bgp.md), the original
  choice) — superseded. Once the pod dataplane is on BGP, MetalLB is a
  redundant second BGP speaker for a job Calico already does; and Calico's
  VXLAN mode, which MetalLB was decoupled from, is gone anyway.
- **The crawl-before-walk plan** (ship VXLAN + MetalLB first, consolidate
  later) — overturned. It would have meant paying the CNI churn *after* Flux
  owned Calico, after node 2 existed, and after workloads had state — every
  one of which makes the flip harder. The dataplane question it was waiting on
  got answered.
- **MetalLB controller only, speaker disabled** (Calico's own recommended
  3.29-era setup: MetalLB allocates, Calico advertises) — **kept as the
  contingency** if Calico's LoadBalancer IPAM misbehaves for some reason other
  than #12890. The dataplane decision stands regardless, and no MetalLB
  *speaker* ever ships.
- **Keeping VXLAN and adding BGP only for LB advertisement** — rejected: pays
  the BGP setup cost without the MTU/CPU win, and leaves two dataplane
  mechanisms to reason about.

## Consequences

- **Repo-wide guardrail: no MetalLB.** Recorded in the root `CLAUDE.md`.
- **Requires Calico ≥ 3.30** for LoadBalancer IPAM (3.29 can only advertise,
  not allocate) — hence the bump in
  [ADR-0019](0019-k3s-1.36-calico-3.32.1-version-pair.md), and the mandatory
  #12890 workaround that ships **with** the BGP CRs, not after. Without it LB
  IPs sit `pending` forever while BGP advertises the routes normally, so the
  BGP side gives no hint.
- **Networking prep is on the critical path, not parallel.** The pfSense/FRR
  side (LB range, ASNs, the peering, RFC 8212 policy) gates the Calico
  migration rather than sitting alongside it.
- **The risk window is node 2, not today.** With one node the mesh has no
  peers, so flipping to no-encap is trivially safe. The first moment mesh
  routing carries real traffic is when node 2 joins — plan that join as a
  dataplane event, not a capacity add. Also: changing an existing IPPool's
  encapsulation is not a clean in-place edit under the Tigera operator; on an
  empty cluster the honest path is to re-prime or rebuild.
- **`natOutgoing` + the filter give the asymmetry we want for free**: pods
  reach the LAN, the LAN has no route back to pods. But this is **route
  hygiene, not enforcement** — nodes still forward for pod IPs, so anything on
  the node subnet that adds a static route reaches pods anyway. Real
  enforcement is Calico `GlobalNetworkPolicy`/`ClusterNetworkPolicy`, which a
  static route can't bypass. Don't confuse the two. Cheap belt-and-braces: the
  pfSense-side inbound prefix list ([ADR-0023](0023-rfc8212-real-policy-le32.md)).
- **The pod CIDR must never appear on pfSense.** Two independent controls
  guard it — the `BGPFilter` and pfSense's prefix list — because the filter
  is enforced by the very device we'd be guarding against misconfiguring. If
  `10.42.0.0/16` shows up in `vtysh -c 'show ip bgp'`, **both** failed.
- Nodes spanning subnets would need FRR to accept the pod CIDR too; on the
  same-subnet design it needs only the LB range.
- **🔁 Revisit trigger for `assignIPs: AllServices` — adding a second
  LoadBalancer IPAM provider.** MetalLB, a cloud controller, kube-vip, or
  anything else that hands out LoadBalancer addresses makes this a conflict,
  and the trigger is *adding the provider*, not waiting for a symptom. The
  conflict is narrower than it looks: Calico **skips** any Service whose
  `spec.loadBalancerClass` is something other than `calico`, so a provider
  that claims its own class is already safe. The real collision is a provider
  that watches *unclassed* Services — then both assign and last-writer-wins,
  showing up as an EXTERNAL-IP that changes by itself or an address from the
  wrong pool that pfSense has no route to. The fix (`RequestedServicesOnly`)
  has its own footgun: setting it without adding `loadBalancerClass: calico`
  turns every existing LB Service `pending` at once. See the header in
  `gitops/infrastructure/calico-bgp/ippool-loadbalancer.yaml`.
- The 2026-07-12 "OPEN" note in the design doc's decision log is closed by this
  ADR.

## Evidence

Verified live 2026-08-16 on a from-scratch rebuild of `snoop-a2o` (VM destroyed
and reprovisioned, so no VXLAN residue and the first-boot path is what got
tested): pod pool `Never / Never` (no IPIP, no VXLAN); BGP session
`Established`; test Service allocated an LB IP immediately (#12890 workaround
works); `HTTP 200` in 10 ms from the LAN segment with hop 1 = pfSense; BIRD
exports exactly `${lb_range}`, pfSense shows `PfxRcd 1`; the pod CIDR absent
from `show ip bgp` — confirmed on both sides independently; `PfxSnt 0`
(pfSense advertises nothing back). Both advertisement modes exercised. Survived
the Flux handover (2026-08-29) and the from-scratch `site.yml` run
(2026-08-30). Full tables in [`../worklog.md`](../worklog.md).
