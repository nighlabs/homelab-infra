# ADR-0012: Load balancer: MetalLB in BGP mode

- **Date:** 2026-07 (initial design); question raised 2026-07-12; superseded 2026-08-02
- **Status:** **Superseded by [ADR-0018](0018-calico-bgp-replaces-metallb.md)** — MetalLB was never deployed
- **Supersedes / related:** [ADR-0010](0010-calico-over-cilium.md); [ADR-0013](0013-ingress-certs-dns-external-access.md) (the Gateway that needs the LoadBalancer IP); `../architecture.md` §4.4 (the replacement)

## Context

Gateway API / NGINX Gateway Fabric needs LoadBalancer IPs, and nothing in the
first draft of the design provided them — that was the original gap. MetalLB
was the obvious fill.

## Decision (historical)

**MetalLB in BGP mode, peering with pfSense's FRR package** — not L2.

- A dedicated **routed** `/24` for services, carved from the reserved LAN
  range by pfSense rather than from node-VLAN host space: cleaner, and it is
  what BGP advertises.
- Two private ASNs from `64512–65534`, one for pfSense and one for MetalLB.
- **Peer all worker nodes**, not a single node, so there is no SPOF.
- Pairs with `externalTrafficPolicy: Local` to preserve real client IPs while
  still distributing across workers.
- FRR gotcha noted at the time: enable "Disable eBGP Require Policy" (or add a
  route-map / prefix-list) or FRR silently refuses MetalLB's routes. (The
  eventual answer was real policy, not the disable — [ADR-0023](0023-rfc8212-real-policy-le32.md).)

## Alternatives rejected (at the time)

- **No load balancer at all.** The gap that started this.
- **L2 mode.** Loses the dedicated routed range, ECMP spread, and faster
  failover across workers that BGP gives.

## Why it was superseded

Raised 2026-07-12, decided 2026-08-02: since the design was *already* standing
up a BGP session to FRR for the LB range, and Calico was *already* the CNI,
Calico's own BGP could advertise LoadBalancer IPs directly — dropping MetalLB
(one fewer tool, one fewer BGP speaker for FRR to manage) — and, if the pod
*dataplane* also moved to BGP, removing VXLAN encapsulation entirely. The
costs weighed were coupling pod networking to the pfSense fabric and relying
on Calico's newer LoadBalancer IPAM. The initial "crawl before walk" plan
(ship Calico VXLAN + MetalLB first, consolidate later) was abandoned once the
upstream question — *do we want the pod dataplane on BGP/no-encap at all?* —
was answered yes, because churning the CNI was cheapest with one node and no
workloads. Full reasoning: [ADR-0018](0018-calico-bgp-replaces-metallb.md).

## Consequences

- **No MetalLB.** Calico BGP owns both LoadBalancer IP advertisement and the
  pod dataplane (root `CLAUDE.md` guardrail).
- The routed-range, two-ASN, and all-nodes-peered ideas survived intact into
  the Calico design; the ASN split and the LB range derivation are in
  [ADR-0026](0026-per-cluster-derivation-from-index.md), the pfSense side in
  [ADR-0022](0022-pfsense-frr-raw-config-explicit-neighbors.md).
- MetalLB's *controller* (allocation only, speaker disabled) remains the
  documented contingency if Calico's LoadBalancer IPAM misbehaves for a reason
  other than the known RBAC bug ([ADR-0019](0019-k3s-1.36-calico-3.32.1-version-pair.md)).
