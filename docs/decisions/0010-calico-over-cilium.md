# ADR-0010: CNI: Calico

- **Date:** 2026-07 (initial design)
- **Status:** Accepted
- **Supersedes / related:** [ADR-0011](0011-cluster-cidrs-never-cgnat.md) (the IPPool must match `cluster-cidr`); [ADR-0016](0016-calico-ansible-primes-flux-adopts.md) (how it is installed); [ADR-0018](0018-calico-bgp-replaces-metallb.md) (Calico BGP later absorbs load balancing and the dataplane); [ADR-0024](0024-calico-ebpf-dataplane-no-kube-proxy.md) (Calico's eBPF dataplane); `../architecture.md` §4.3

## Context

k3s ships flannel and kube-router's NetworkPolicy controller. The design
wants NetworkPolicy enforcement to segment namespaces (closing the workload-
isolation gap) and, as it turned out, BGP — so a CNI with real policy and a
real BGP speaker was needed. The two serious candidates were Calico and
Cilium.

## Decision

**Calico**, installed with k3s configured `flannel-backend: none` and
`disable-network-policy: true` (the latter only turns off k3s's built-in
kube-router controller — NetworkPolicies still work, enforced by Calico).
Calico's IPPool must match `cluster-cidr` (`10.42.0.0/16`).

## Alternatives rejected

- **Cilium.** A prior hard struggle to set it up and configure it. Calico
  delivers CNI + NetworkPolicy without Cilium's eBPF-stack setup friction.
  The original note added "revisit eBPF only if you later drop kube-proxy."

That revisit happened — and the outcome is worth stating precisely, because
"Calico over Cilium" is *not* "iptables over eBPF." The cluster now runs
**Calico's own eBPF dataplane with kube-proxy removed**
([ADR-0024](0024-calico-ebpf-dataplane-no-kube-proxy.md)). The choice was
never about eBPF's merits; it was about **reversibility**. Calico's dataplane
is a switch — `linuxDataplane: Iptables` reverts it, documented and supported,
with policy semantics, CRDs, IPAM, and BIRD all unchanged — so trying eBPF was
a cheap experiment with a known exit. An eBPF-only CNI offers no such exit,
and the earlier struggle was precisely the cost of having no way back from its
setup problems. Argue this choice on reversibility, not on benchmarks.

## Consequences

- Calico owns both the pod dataplane and NetworkPolicy; keep k3s's flannel and
  its policy controller disabled.
- Pod isolation, when it arrives, uses Calico `GlobalNetworkPolicy` /
  `ClusterNetworkPolicy` — never the deprecated AdminNetworkPolicy path, which
  Calico v3.32 replaced ([ADR-0019](0019-k3s-1.36-calico-3.32.1-version-pair.md)).
- Calico pins to `nodeAddressAutodetectionV4.kubernetes: NodeInternalIP`,
  which k3s advertises as the DMZ/eth0 address. That keeps the real DMZ subnet
  out of Git and guarantees Calico never lands on the Ceph NIC (eth1).
- The pod CIDR (`10.42.0.0/16`) is the one network value committed in the
  Calico values; the bootstrap play asserts it equals `k3s_cluster_cidr` so
  the two cannot drift.
- Choosing Calico is what later made [ADR-0018](0018-calico-bgp-replaces-metallb.md)
  possible: a CNI that already speaks BGP could take over LoadBalancer
  advertisement and IPAM, removing MetalLB.
