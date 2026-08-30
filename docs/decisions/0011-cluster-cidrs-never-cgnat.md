# ADR-0011: Cluster CIDRs live in 10.0.0.0/8 — never CGNAT 100.64.0.0/10

- **Date:** 2026-07 (initial design)
- **Status:** Accepted
- **Supersedes / related:** [ADR-0010](0010-calico-over-cilium.md); [ADR-0013](0013-ingress-certs-dns-external-access.md) (Tailscale is in the design, which is why this matters); [ADR-0026](0026-per-cluster-derivation-from-index.md) (the LoadBalancer range, which is *not* a cluster-internal CIDR); `../architecture.md` §4.2; root `CLAUDE.md` guardrails

## Context

The existing LAN's RFC 1918 range is reserved — LAN, Proxmox, and Ceph all
live there (real value in Bitwarden Secrets Manager, never in Git). Cluster-
internal ranges must not overlap it. A common recommendation is to put cluster
CIDRs in the CGNAT range `100.64.0.0/10` on the theory that it "avoids RFC 1918
conflicts."

## Decision

- **Cluster-internal CIDRs live in `10.0.0.0/8`**, which is free here, and are
  **pinned explicitly** in every node's k3s config rather than left as implicit
  defaults: `cluster-cidr: 10.42.0.0/16`, `service-cidr: 10.43.0.0/16` (the k3s
  defaults, made non-implicit).
- **Calico's IPPool must match `cluster-cidr`** (`10.42.0.0/16`). The
  bootstrap play asserts the equality before priming Calico.
- **Do NOT use CGNAT (`100.64.0.0/10`) for any cluster CIDR.**

## Alternatives rejected

- **CGNAT `100.64.0.0/10`.** A trap in *this* stack, whatever its merits
  elsewhere:
  - **Tailscale** installs a route for the whole `/10` and drops `100.64/10`
    traffic that does not arrive on `tailscale0`. Tailscale is part of the
    design ([ADR-0013](0013-ingress-certs-dns-external-access.md)), so this is
    a guaranteed collision, not a hypothetical one.
  - **Cloudflare** reserves `100.64/12`, `100.80/16`, `100.96/12`, and
    `100.112/16`.
  The "avoids RFC 1918 conflicts" argument does not apply here — CGNAT
  actively collides with two services the stack depends on.
- **Carving cluster CIDRs out of the reserved LAN range.** Reused address
  space; nothing to gain, and it is the range every other lab host already
  occupies.

## Consequences

- Repo-wide guardrail: **no CGNAT for any cluster CIDR**. Cluster CIDRs stay
  in `10/8`.
- The pod CIDR `10.42.0.0/16` is deliberately public — the one network value
  that appears in cleartext in committed manifests and docs. Everything else
  address-shaped is topology and stays blinded ([ADR-0021](0021-topology-blinding-postbuild-substitution.md)).
- The **LoadBalancer range is a separate matter**: it is a routed-only prefix
  that pfSense learns over BGP, derived per cluster from a blinded supernet
  ([ADR-0026](0026-per-cluster-derivation-from-index.md)). It is not a cluster-
  internal CIDR and this ADR does not place it.
- The FRR render play asserts that the derived LB ranges do not overlap the
  DMZ subnet, the Ceph public network, or the pod/service CIDRs.
