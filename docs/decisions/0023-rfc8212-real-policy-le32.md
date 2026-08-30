# ADR-0023: Satisfy FRR's RFC 8212 requirement with real prefix lists (`<CLUSTER>-IN permit <lb_range> le 32`, `-OUT deny any`) — never `no bgp ebgp-requires-policy`

- **Date:** 2026-08-02 (decided) · 2026-08-16 (`le 32` trap found; both verified live)
- **Status:** Accepted
- **Supersedes / related:** replaces the "enable *Disable eBGP Require Policy*" advice in [ADR-0012](0012-metallb-bgp.md); [ADR-0022](0022-pfsense-frr-raw-config-explicit-neighbors.md) (the config this lives in), [ADR-0018](0018-calico-bgp-replaces-metallb.md) (the `BGPFilter` on the other side), [ADR-0024](0024-calico-ebpf-dataplane-no-kube-proxy.md) (why `Cluster` is the default policy, and so why `le 32` still matters). Runbook: [`../pfsense-frr-bgp-setup.md`](../pfsense-frr-bgp-setup.md) §4.

## Context

FRR 7.4+ implements RFC 8212: an eBGP session with **no** inbound/outbound
policy discards all routes **in both directions** — while still reporting
`Established` with no prefixes moving. This is the most common silent failure
in this kind of setup. Every MetalLB-era guide, and the original design
([ADR-0012](0012-metallb-bgp.md)), says to tick the GUI's "Disable eBGP Require
Policy" (`no bgp ebgp-requires-policy`).

The stakes went up with [ADR-0018](0018-calico-bgp-replaces-metallb.md): with
the pod dataplane on BGP, a silent refusal is no longer only an
LB-reachability bug.

## Decision

**Option B — apply actual prefix lists. The requirement is satisfied *because
policy exists*; the disable is omitted entirely.**

```
ip prefix-list HOMELAB-IN seq 10 permit ${lb_range} le 32
ip prefix-list HOMELAB-IN seq 20 deny any
ip prefix-list HOMELAB-OUT seq 10 deny any
```

- `<CLUSTER>-IN` permits only that cluster's LB range. `<CLUSTER>-OUT` denies
  everything — the nodes get their default route statically from Ignition and
  need to learn nothing from pfSense, and the LAN table can never leak into
  the cluster.
- **Both directions must be populated.** An inbound filter alone still gets
  outbound routes discarded.
- The filters are per-cluster because they hang off the peer group, which is
  what separates clusters on a shared subnet
  ([ADR-0022](0022-pfsense-frr-raw-config-explicit-neighbors.md)).

**Why B, given A is one checkbox:** we want the inbound prefix list regardless,
as defence in depth. The Calico-side `BGPFilter` is the primary control
keeping the pod CIDR off pfSense — but it is enforced by the very device we're
guarding against misconfiguring. A pfSense-side prefix list is an
*independent* check: it guarantees pfSense never installs a route to
`10.42.0.0/16` even if the `BGPFilter` is wrong or missing. Once it exists,
disabling the requirement buys nothing and throws away a safety net.

### `le 32` is load-bearing, and its absence fails silently

FRR matches the prefix length *exactly* unless given `le`/`ge`: *"In the case
of no le or ge command, the prefix length must match exactly the length
specified in the prefix list."* Calico's advertisement granularity is not
fixed — it follows the Service's `externalTrafficPolicy`:

| Policy | What Calico advertises | Matched by bare `permit …/24`? |
|---|---|---|
| `Cluster` | the **whole block**, from every node | ✓ |
| `Local` | a **/32 per Service**, from nodes holding a backend | ✗ — **dropped** |

So a bare `permit ${lb_range}` works right up until the first `Local` Service,
which then establishes a perfectly healthy-looking session and blackholes.
`le 32` covers both modes and costs nothing. (The Calico-side `BGPFilter` uses
`matchOperator: In` for the same reason, other end of the wire.)

## Alternatives rejected

- **Option A — `no bgp ebgp-requires-policy`** (the GUI checkbox, what most
  guides say) — **rejected as the standing config.** Acceptable only as a
  temporary bisect step when isolating a bring-up fault, then reverted.
- **Inbound filter only** — outbound routes still discarded under RFC 8212.
- **Bare `permit <lb_range>` without `le 32`** — the silent-blackhole mode
  above.
- **Trusting the `BGPFilter` alone** — it's enforced by the device being
  guarded against.

## Consequences

- **Two different causes now produce "Established session, nothing flowing"**:
  RFC 8212 refusal, and a prefix list without `le 32`. Both present as a
  healthy session. **Session state is not proof of anything; the test is
  reachability** — reach an actual LoadBalancer IP from another segment.
- **The `/32` acceptance is the one check `curl` cannot make.** At one node
  the `/32` is redundant for reachability (the address stays reachable via the
  block), so a broken filter is invisible to any connectivity test and visible
  only in `show ip bgp`. At two nodes it stops being cosmetic: the `/32` is
  what steers traffic to the node actually holding the backend, and without it
  ECMP scatters across nodes that have no local pod and `Local` drops it. **Do
  not "simplify" `le 32` away.**
- Acceptance: `show ip bgp` contains the LB range and **no pod CIDR** — if
  `10.42.0.0/16` appears, *both* the `BGPFilter` and `<CLUSTER>-IN` failed;
  `advertised-routes` is empty for every neighbor.
- When verifying a paste, confirm `le 32` survived FRR's config rewrite
  ([ADR-0022](0022-pfsense-frr-raw-config-explicit-neighbors.md)).

## Evidence

Verified live 2026-08-16: FRR reports `Inbound path policy configured` /
`Outbound path policy configured`; the session reached `Established` and
`PfxRcd 1` proves prefixes traverse it; `no bgp ebgp-requires-policy` is not
present. Both advertisement modes exercised on one cluster — a `Cluster`
Service rode the `/24` block, a `Local` Service produced `x.x.x.130/32` and
pfSense **accepted** it. Both routes withdrew cleanly on teardown. See
[`../worklog.md`](../worklog.md).
