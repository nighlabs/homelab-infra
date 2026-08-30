# ADR-0026: Everything cluster-scoped derives from one number — cluster `index` → ASN and LB range; join token, TLS SANs and k3s version are per-cluster; the LB range is routed-only

- **Date:** 2026-08-16 (decided; values settled; verified live)
- **Status:** Accepted
- **Supersedes / related:** [ADR-0017](0017-static-addressing-no-dhcp.md) (the same principle at node scope: `node_number` derives everything), [ADR-0018](0018-calico-bgp-replaces-metallb.md), [ADR-0022](0022-pfsense-frr-raw-config-explicit-neighbors.md), [ADR-0023](0023-rfc8212-real-policy-le32.md), [ADR-0021](0021-topology-blinding-postbuild-substitution.md) (how the derived values reach Git-committed manifests). Runbook: [`../pfsense-frr-bgp-setup.md`](../pfsense-frr-bgp-setup.md) §1. Code: `ansible/inventory/nodes.yml`, `ansible/inventory/group_vars/all/vars.yml`, `ansible/roles/flatcar_vm/tasks/preflight.yml`, `ansible/playbooks/render-frr-config.yml`.

## Context

The BGP work was blocked on four values: the LB `/24`, the FRR peer address,
and the two ASNs. Separately, the k3s join token and TLS SANs were single
fleet-wide values, and `k3s_minor` was stated by hand alongside `k3s_version`.
Each of these was a place where two things had to be kept consistent by hand,
or where one cluster's value leaked into another's.

## Decision

### Per-cluster values derive from `index`

Each cluster in `inventory/nodes.yml` carries one `index:`; its ASN and its
LoadBalancer range both derive from that number, the same way every
host-shaped fact derives from `node_number`:

| Value | Derivation | `homelab` (index 1) |
|---|---|---|
| Cluster ASN | `bgp_asn_base + index` | `64601` |
| LoadBalancer range | `<lb_range_base>.<index>.0/24` | index 1 of that supernet |
| pfSense ASN | fixed, one per *router* | `64512` |
| pfSense peer IP | `dmz_network.gateway` | already in BWS |

Adding a cluster means adding an `index:` — not editing four variables and
hoping they stay consistent. Only **two** new secrets were needed
(`lb_range_base`, `frr_master_password`); both ASNs are cleartext constants
(`bgp_peer_asn: 64512`, `bgp_asn_base: 64600` — leaving 64512–64599 free and
keeping cluster ASNs visually distinct from pfSense's). A number from the RFC
6996 private range reveals nothing about the environment.

**The peer IP is the DMZ gateway.** pfSense's BGP address *is* its DMZ
interface address, which *is* the nodes' default gateway — one value, already
in BWS, no second variable to drift. ⚠ That equivalence breaks under **CARP**:
the gateway would be a VIP while BGP must peer with the physical interface
address. No CARP on that interface today (confirmed 2026-08-16); if that
changes, `bgp_peer_ip` becomes its own secret.

**Nodes did not move**, so nothing was re-provisioned.

### Two ASNs, and every node in a cluster shares its cluster's one

Two distinct ASNs make each pfSense session **eBGP** — the mode with AS_PATH
loop prevention and the one FRR's RFC 8212 policy requirement applies to
([ADR-0023](0023-rfc8212-real-policy-le32.md)). iBGP would need a route
reflector or full mesh for a two-party peering — pointless. The in-cluster
node-to-node mesh is then iBGP (all nodes one AS, full mesh — exactly what
`nodeToNodeMeshEnabled` provides).

There is no per-node AS assignment anywhere; the numbers are stated exactly
twice each, once per side: pfSense `router bgp 64512` + `neighbor <cluster>
remote-as <cluster ASN>`; Calico `BGPConfiguration.spec.asNumber` = the
cluster ASN + `BGPPeer.spec.asNumber` = `64512`.

- ⚠ **Do not use the per-node ASN override** (`Node.spec.bgp.asNumber`). It
  exists for AS-per-rack topologies; here it would turn the mesh into eBGP
  *and* force `bgp bestpath as-path multipath-relax` on pfSense before ECMP
  would work at all (without it the *entire* AS_PATH must match, not just its
  length — byte-identical paths from a shared ASN is what lets us omit it).
- ⚠ **Calico's default `asNumber` is `64512`** — the same number we gave
  pfSense. Set the cluster's ASN explicitly. A cluster landing on 64512 would
  make the session iBGP and behave differently; the render playbook asserts
  against this specific collision because it's a live trap, not a theoretical
  one.

### The LB range is routed-only

The LB range is **not** a subnet of the DMZ, and must be assigned to **no
interface anywhere** — not a pfSense interface, not a VLAN, not a DHCP pool. It
exists only as a BGP-learned route. An earlier revision of the runbook called
for "a /24 inside the DMZ subnet"; that fails two ways, neither obvious:

- pfSense has a **connected** route for the DMZ. A BGP route of equal length
  loses to it on administrative distance, so pfSense forwards onto the DMZ
  segment instead of to a node.
- On-subnet clients skip routing entirely and **ARP** for the LB address.
  Nothing answers — Calico does no L2 for LoadBalancer IPs. That's MetalLB's
  L2 mode, which we don't run.

A range that exists nowhere else also makes the old "outside the DHCP pool"
constraint moot.

### Join token, TLS SANs and k3s version are per-cluster

`k3s_tokens` / `k3s_tls_sans_by_cluster` are maps keyed by cluster name
(BWS secrets `k3s_token_<cluster>`, `k3s_tls_sans_<cluster>`), resolved from
`node.cluster`. The token is the credential that admits a node to a cluster,
so one shared value made a leak from any cluster a leak for all of them — and
k3s derives the datastore bootstrap-data encryption key from it too. TLS SANs
are per-cluster by construction: a stable API name or VIP belongs to one
cluster, and a shared list puts cluster B's name in cluster A's cert.
Migration was a no-op for a running node: move the old value under the cluster
key and the rendered config is byte-identical. **There is deliberately no
fallback to the old variable** — a loud assert beats a silent half-migration.

`k3s_version_default` is the fleet default; a cluster overrides it with
`k3s_version:` in its `nodes.yml` block, which is what lets a minor bump be
staged on one cluster while another stays put. **`k3s_minor` is derived** from
the effective version (`v1.36.2+k3s1` → `v1.36`) instead of stated separately
— the two always had to satisfy minor ⊂ version, and keeping that by hand is a
two-place edit that drifts. An explicit per-cluster `k3s_minor:` still wins,
and preflight asserts containment on both paths. Why that assert matters: if
the seeded sysext falls outside the sysupdate `MatchPattern`, the node boots
the *correct* k3s and then silently never updates. Nothing looks wrong at
provision time. (Calico is deliberately *not* per-cluster —
[ADR-0019](0019-k3s-1.36-calico-3.32.1-version-pair.md).)

## Alternatives rejected

- **Four independently chosen BGP values** — four things to keep consistent
  across Ansible, Calico and pfSense, with drift detectable only by a session
  that never establishes.
- **A separate `bgp_peer_ip` secret** — a second copy of the DMZ gateway; kept
  in reserve only for the CARP case.
- **Per-node ASNs** — see above.
- **The LB `/24` carved from the DMZ subnet** — wrong twice over, as above.
- **Fleet-wide token with a fallback for the old name** — the fallback would
  let a half-migrated inventory run silently.
- **Stating `k3s_minor` explicitly** — a two-place edit that drifts.

## Consequences

- **`render-frr-config.yml` asserts before rendering:** every cluster declares
  an `index`, unique, in 1..254; cluster ASNs are in 64512–65534, unique, and
  different from pfSense's; no derived LB range overlaps the DMZ subnet, the
  Ceph public network, or the pod/service CIDRs. These were verified *firing*,
  not just passing. It cannot see the pfSense interface list, so the "attached
  to no interface" half stays a manual check.
- **The per-cluster facts are set unconditionally**, not under `when:
  k3s_enabled`. `set_fact` persists across the role's per-node loop, so a
  `when` would leave a non-k3s node holding the *previous* node's token — the
  same stale-registered-value trap as the snippet-dir re-stat. Verified on
  that exact case (a bare VM provisioned after two k3s nodes from different
  clusters resolves to `''`), not just the happy path.
- Adding a cluster: another key under `clusters:` with its own `index` and
  non-overlapping `node_number`s, plus one BWS secret `k3s_token_<cluster>`.
  Nothing else in the repo changes. (Untested against real hardware — only one
  cluster exists.)
- All five derived/shared values reach Git-committed manifests as
  `${placeholders}` ([ADR-0021](0021-topology-blinding-postbuild-substitution.md)),
  which is what keeps the derivation single-sourced.

## Evidence

Rendering verified for one and two clusters; asserts verified firing;
`snoop-a2o` resolves to `v1.36.2+k3s1`/`v1.36` exactly as before with no
re-provision; the BGP session using the derived ASN and LB range verified live
2026-08-16. See [`../worklog.md`](../worklog.md).
