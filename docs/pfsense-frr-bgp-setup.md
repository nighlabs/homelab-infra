# Configuring FRR/BGP on pfSense CE 2.8.1-RELEASE

Peers pfSense with the k3s cluster so Calico can advertise **LoadBalancer
service IPs** into the LAN. Companion to `ansible/CLAUDE.md` §7 items 6 + 8;
the Calico side lives in `gitops/CLAUDE.md` ("Calico BGP — CRs, not Helm
values").

> **✅ DECIDED 2026-08-02 — the FRR configuration is managed as raw config, not
> through the GUI.** Two capabilities we want (`bgp listen range` and
> `maximum-paths`) exist *only* there. The GUI's routing config is ignored
> wholesale once raw config is saved, so this is an all-or-nothing choice and
> we're taking it deliberately. Rationale and consequences: §4.

> **⚠ This runs on live production gear.** The same pfSense box routes
> everything else in the house. Every step below is additive and parked —
> nothing changes existing routing until a Calico node actually peers. Read §8
> before starting.

**Blinding rule:** this document is committed, so it contains **no real
addresses or ASNs**. Values appear as `${placeholder}` matching the vault
variable names. Fill them from `ansible/inventory/group_vars/all/vault.yml`.
Do not paste real values back into this file.

---

## 1. Values to decide first

These are the open blockers from `ansible/CLAUDE.md` §7 item 6. Decide them all
before touching pfSense — they're inputs to both sides of the peering, and a
mismatch just yields a session that never establishes.

| Value | Vault variable | Constraint |
|---|---|---|
| Cluster ASN | `vault_calico_asn` | Private range **64512–65534** |
| pfSense ASN | `vault_bgp_peer_asn` | Private range, **different** from the cluster ASN |
| pfSense peer IP | `vault_bgp_peer_ip` | pfSense's **DMZ interface** address |
| LoadBalancer range | `vault_lb_range` | A `/24` inside the DMZ subnet, **outside** the DHCP pool and any static assignments |
| DMZ subnet (CIDR) | derived from `vault_dmz_subnet_base` | The listen range (§4); already vaulted |
| FRR master password | `vault_frr_master_password` | A **credential** — BWS-managed, never in a committed manifest |

Two distinct ASNs makes this **eBGP**, which is what we want: it's the mode with
AS_PATH loop prevention, and the mode FRR's policy requirement applies to (§4).
iBGP would need a route reflector or full mesh to distribute anything, which is
pointless for a two-party peering.

The LB range must not overlap anything pfSense already hands out. Calico owns
these addresses via BGP advertisement; if DHCP also leases one you get an
intermittent duplicate-address failure that looks like a BGP problem and isn't.

### Where the two ASNs actually get set

**Every k3s node shares one ASN.** There is no per-node AS assignment anywhere —
the two numbers are each stated twice, once per side, and that's the complete
list:

| # | Where | Setting |
|---|---|---|
| 1 | pfSense | `router bgp ${bgp_peer_asn}` |
| 2 | pfSense | `neighbor k3s remote-as ${calico_asn}` (on the peer group) |
| 3 | Calico | `BGPConfiguration.spec.asNumber` = `${calico_asn}` |
| 4 | Calico | `BGPPeer.spec.asNumber` = `${bgp_peer_asn}` |

Nodes never appear individually. On pfSense the listen range admits them into
the peer group, which carries the remote AS. On the Calico side
`BGPConfiguration.spec.asNumber` is the cluster-wide default every node
inherits, and `BGPPeer` is global (no `nodeSelector`), so it needs no per-node
entry either.

This makes the in-cluster mesh **iBGP** (all nodes one AS, full mesh — exactly
what `nodeToNodeMeshEnabled` provides) and the pfSense session **eBGP**.

⚠ **Do not use the per-node ASN override** (`Node.spec.bgp.asNumber`). It exists
for AS-per-rack topologies; here it would turn the mesh into eBGP and require
per-node config on both sides for no benefit.

⚠ **Calico's default `asNumber` is `64512`.** Set it explicitly even if you pick
that value. If pfSense also lands on 64512 the session becomes iBGP and behaves
differently — the two **must** differ.

---

## 2. Install the FRR package

**System > Package Manager > Available Packages**, search `frr`.

pfSense CE **2.8.1** predates the FRR 10 package — per
[Todo #15785](https://redmine.pfsense.org/issues/15785) that targets CE
**2.9.0** / Plus 25.11, so you're on the earlier branch
([#13575](https://redmine.pfsense.org/issues/13575) moved it to 9.0.1).

Confirm what you actually got, since everything in §4 assumes FRR ≥ 7.4:

```sh
pkg info | grep -i frr
vtysh -c 'show version'
```

Package versions move independently of the base release, so read the output
rather than assuming. Anything in 8.x / 9.x / 10.x behaves identically here.

---

## 3. Enable the daemons (the one part the GUI still owns)

⚠ **Raw config supplies the *configuration*, but the GUI still decides which
daemons run.** The BGP tab's Enable is documented as the *"Master enable switch
for BGP routing"* — leave it off and `bgpd` never starts, so your raw config is
never read and the failure is silent. This is the single most likely way to get
a correct config that does nothing.

**Services > FRR Global Settings**, *Global Settings* tab:

- **Enable FRR** — check. Master switch; unchecked disables all of FRR.
- **Master Password** — required. This is FRR's internal daemon password, not a
  BGP peer password. Vault it as `vault_frr_master_password`; it's a credential,
  so per the root `CLAUDE.md` it belongs in BWS and never in Git.
- **Default Router ID** — leave unset; §4 sets it per-protocol.

**Services > FRR BGP**, *BGP* tab:

- **Enable** — check. This starts `bgpd`.
- **Local AS** — `${vault_bgp_peer_asn}`
- **Router ID** — `${vault_bgp_peer_ip}`

Leave OSPF and OSPF6 disabled — we only want BGP.

> The Local AS and Router ID above are **ignored** once raw config is saved (§4
> sets them). Fill them in anyway: it costs nothing, and it means the GUI state
> is coherent rather than nonsense if anyone ever clears the raw config to fall
> back.

---

## 4. The FRR configuration

**Services > FRR Global Settings**, *Raw Config* tab → **Saved frr.conf**.

### Why raw, and what it costs

The GUI cannot express two things we want:

| | GUI | Raw config |
|---|---|---|
| `bgp listen range` (dynamic neighbors) | ✗ | ✓ — node adds need **zero** pfSense changes |
| `maximum-paths` (ECMP) | ✗ | ✓ |
| Per-node neighbor rows | required | none |
| Config reviewable in Git | ✗ | ✓ — as a template (§9) |
| GUI routing config still applied | ✓ | **✗** |

`bgp listen range` is a longstanding gap (the equivalent OPNsense requests
[#4015](https://github.com/opnsense/plugins/issues/4015) /
[#4713](https://github.com/opnsense/plugins/issues/4713) are still open);
`maximum-paths` is [Feature #16278](https://redmine.pfsense.org/issues/16278),
open and unassigned. `vtysh` is not an alternative for either — per that report,
*"Manual CLI changes via vtysh work until the next GUI 'Apply,' which overwrites
them."*

**The cost is total and confirmed:** *"If you are using Raw-Config to add
commands, the GUI will not be able to control the configuration. You need to
delete Raw-Config and add the configuration via GUI only."* There is no warning
banner on the pages that quietly stop working.

That's an acceptable trade here because the config is ~25 lines. Owning it
outright is a smaller commitment than "hand-managed firewall" implies, and a
committed template is *more* reviewable and reproducible than GUI forms, which
are unversioned by definition.

### The config

Placeholders match the vault variables in §1.

```
frr defaults traditional
service integrated-vtysh-config
log syslog informational
password ${frr_master_password}
!
router bgp ${bgp_peer_asn}
 bgp router-id ${bgp_peer_ip}
 bgp log-neighbor-changes
 timers bgp 3 9
 !
 ! Dynamic neighbors: any k3s node in the DMZ joins automatically.
 neighbor k3s peer-group
 neighbor k3s remote-as ${calico_asn}
 bgp listen range ${dmz_subnet_cidr} peer-group k3s
 !
 address-family ipv4 unicast
  neighbor k3s activate
  ! RFC 8212 is satisfied by HAVING policy — see below.
  neighbor k3s prefix-list K3S-IN in
  neighbor k3s prefix-list K3S-OUT out
  maximum-paths 8
 exit-address-family
!
! Inbound: accept ONLY the LoadBalancer range.
ip prefix-list K3S-IN seq 10 permit ${lb_range}
ip prefix-list K3S-IN seq 20 deny any
!
! Outbound: advertise nothing to the cluster.
ip prefix-list K3S-OUT seq 10 deny any
!
```

### Line-by-line rationale

**`timers bgp 3 9`** — keepalive 3s, hold 9s. The default 180s hold means up to
three minutes of blackholing on node failure. The **negotiated hold time is the
minimum of the two peers' values**, so setting it here governs the session
regardless of Calico's default — no cluster-side change needed. See §10 for why
BFD isn't the answer.

**`bgp listen range … peer-group k3s`** — the whole reason for raw config. Any
node in the DMZ subnet that initiates a session is admitted and inherits the
peer group's remote-AS and filters. Adding a k3s node requires **no pfSense
change at all**.

**No `no bgp ebgp-requires-policy`.** FRR 7.4+ implements RFC 8212: an eBGP
session with no policy discards all routes in both directions while still
reporting `Established`. Most guides disable the requirement. We instead
*satisfy* it with the two prefix lists — we want the inbound filter anyway, and
once it exists, disabling the requirement only discards a safety net. **Both
directions must be populated**; an inbound filter alone still gets outbound
routes discarded.

**`K3S-IN`** is the real security boundary. It guarantees pfSense never installs
a route to the pod CIDR (`10.42.0.0/16`) even if the Calico-side `BGPFilter` is
wrong or missing. The `BGPFilter` is enforced by the very device we'd be
guarding against misconfiguring, so it isn't trusted alone — these are two
independent controls.

**`K3S-OUT` denies everything.** The k3s nodes get their default route
statically from Ignition (repo rule: no DHCP in cluster networking), so they
need to learn nothing from pfSense. This also means the LAN table can never leak
into the cluster.

**`maximum-paths 8`** — ECMP across nodes advertising the same LB prefix. Inert
at one node. See §10 for what it does and doesn't buy.

**No `bgp bestpath as-path multipath-relax`** — deliberately. ECMP across eBGP
peers normally needs it, because without it the *entire* AS_PATH must match, not
just its length. Since every node shares one ASN (§1), the AS_PATH from all of
them is byte-identical. Another reason to avoid per-node ASNs: they'd require
this too.

### ⚠ Verify the password line

The raw config replaces the generated `frr.conf` wholesale, so the master
password must be *in it*. Whether the package still injects it independently is
worth confirming on first apply rather than assuming — if `vtysh` works and the
daemons are healthy (§7), you're fine.

---

## 5. Apply it

1. Paste the rendered config into **Saved frr.conf** and save.
2. Restart FRR (**Status > Services**, or toggle Global Settings Enable).
3. Confirm it actually loaded — the GUI accepting the paste is not proof:

```sh
vtysh -c 'show running-config'
```

If the running config doesn't match what you pasted, **stop**.
[Bug #7859](https://redmine.pfsense.org/issues/7859) was a case where a
config-tag rename caused raw config to be *silently ignored*, so this is a real
failure mode, not a formality.

---

## 6. Firewall rule

**Firewall > Rules > [DMZ interface]**. BGP is TCP/179 and pfSense's own
listening services are not exempt from interface rules:

| Field | Value |
|---|---|
| **Action** | Pass |
| **Protocol** | TCP |
| **Source** | the k3s node addresses (an alias is tidiest) |
| **Destination** | This Firewall (self) |
| **Destination Port** | 179 (BGP) |

Source-restrict it. This matters more with a listen range than it would with
explicit neighbors: FRR will accept a session from *anything* in the range, so
the firewall rule is doing real work in bounding who can try.

Separately, once LB routes are being learned, traffic *to* the LB range from
other segments needs its own pass rules. Learning a route and being permitted to
use it are different things — a working BGP session plus a missing firewall rule
looks exactly like a broken BGP session from a client.

---

## 7. Verification

⚠ **With a listen range, a parked config shows _no neighbors at all_.** Dynamic
neighbors don't exist until a node connects — unlike explicit neighbors, which
would sit visibly in `Active`/`Connect`. An empty `show bgp summary` before the
cluster exists is **success**, not a fault. Don't go hunting.

Pre-cluster, verify only that the daemon is up and the config loaded:

```sh
# bgpd actually running (§3 — the silent-failure check)
vtysh -c 'show version'

# The config in force IS the config we pasted (§5)
vtysh -c 'show running-config'

# Listening at all
sockstat -4 -l | grep 179

# The listen range is registered
vtysh -c 'show bgp listeners'
```

Post-cluster:

```sh
# Dynamic neighbors appear here once nodes connect
vtysh -c 'show bgp summary'

# What we LEARNED — must contain the LB range and nothing else
vtysh -c 'show ip bgp'

# What we ADVERTISE — must be empty
vtysh -c 'show ip bgp neighbors ${node_ip} advertised-routes'

# Negotiated hold time actually in force (min of both sides — §4)
vtysh -c 'show bgp neighbors ${node_ip}' | grep -i 'hold time'

# Multi-node: how many paths were installed (ECMP working?)
vtysh -c 'show ip bgp ${lb_range}'
```

**Status > FRR** gives the same information in the GUI. Note the *Raw Config*
tab's **Update Running** button reads the live config back — useful for
confirming what's actually loaded.

### Acceptance criteria

1. `bgpd` is running and `show running-config` matches the pasted config.
2. TCP/179 listening; firewall rule passes from node addresses only.
3. Pre-cluster: `show bgp summary` empty (expected).
4. Post-cluster: every k3s node appears as a dynamic neighbor, `Established`.
5. `show ip bgp` contains the LB range — **and no pod CIDR**. If `10.42.0.0/16`
   appears, *both* the Calico `BGPFilter` and `K3S-IN` failed; stop and fix.
6. `advertised-routes` is empty for every neighbor.

### 🔁 Re-verify after every FRR package update

Raw config is more fragile across upgrades than GUI config, precisely because
the package isn't regenerating it. Re-run steps 1–2 after any FRR package or
pfSense upgrade rather than assuming it survived.

---

## 8. Blast radius and rollback

Why this is safe to stage ahead of the cluster:

- **Nothing is redistributed.** `K3S-OUT` denies everything and no `network`
  statements exist, so pfSense advertises no routes and no existing routing
  decision changes.
- **No session, no routes.** Until a Calico node connects, the config is inert.
- **Learned routes are filtered to one `/24`** that is otherwise unused.

The one genuinely global change is **Enable FRR** (§3), which starts the
daemons. On a box that has never run FRR that's a new listening service, not a
change to existing forwarding.

**Failure mode is contained:** a syntax error means FRR doesn't start — i.e. no
BGP. Static and kernel routing are untouched, so the box keeps routing.

**Rollback:** uncheck **Enable FRR**. Routing reverts immediately. The raw config
persists harmlessly for a retry.

**To return to GUI management** (should you ever want to): clear the *Saved
frr.conf* field entirely. GUI settings resume being applied only once it's empty.

**Take a config backup first** — **Diagnostics > Backup & Restore** — so a
restore is a known-good path rather than undo-by-memory.

---

## 9. Repo integration

### The config is a template, not a paste

Commit `frr.conf.j2` with `{{ }}` placeholders; Ansible renders it from the
vault. This is the same Jinja2-from-vault pattern the repo already uses for
Ignition, and it's why raw config is *better* for us than the GUI rather than
merely more capable — the GUI's state is unversioned by construction.

The template is committed; the rendered file never is. Paste into the GUI is
manual (pfSense CE ships no API by default), but with a listen range this is a
rare event rather than a per-node one.

**Put a banner at the top of the rendered config** noting the GUI is inert and
edits belong in the template. That's the failure mode most likely to bite a
future reader — including us.

### Vault additions

```yaml
vault_calico_asn: <cluster ASN>
vault_bgp_peer_asn: <pfSense ASN>
vault_bgp_peer_ip: "<pfSense DMZ address>"
vault_lb_range: "<LB /24>"
vault_frr_master_password: "<FRR master password>"
```

These flow to the cluster as the `cluster-topology` Secret, consumed via Flux
`postBuild.substituteFrom` — so committed manifests keep `${bgp_peer_ip}` /
`${bgp_peer_asn}` placeholders and nothing environment-revealing lands in Git.
See `gitops/CLAUDE.md` ("Topology blinding").

`vault_frr_master_password` is a **credential**, not topology — BWS-managed, and
it never reaches a committed manifest.

Remember the strict-substitution gate: an undefined `${var}` becomes an empty
string and reconciles **successfully**, so a typo yields `peerIP: ""` reported
healthy.

---

## 10. Design notes — what this config chose, and why

Mostly inert at one node; determines the shape of the second.

### ECMP decides the node, not the pod

A common misread: **ECMP spreads traffic across _nodes_. It does not decide
which _pod_ serves it.** Two independent layers:

1. **BGP/ECMP** (pfSense → node) — spreads flows across nodes advertising the
   route. This is `maximum-paths`.
2. **kube-proxy** (node → pod) — distributes to replicas once traffic lands.
   Works regardless of BGP.

`maximum-paths` is the *only* thing providing node-level distribution. Without
it there is **no** node-level load balancing in either policy — one node wins
best-path and takes 100% of ingress. What differs is how far traffic spreads
after landing:

| | Node-level | Pod-level | Source IP |
|---|---|---|---|
| **`Cluster` + no ECMP** | ✗ one node takes all | ✓ **all replicas, cluster-wide** (kube-proxy forwards off-node) | lost |
| **`Cluster` + ECMP** | ✓ per-flow across nodes | ✓ all replicas | lost |
| **`Local` + no ECMP** | ✗ one node takes all | ⚠ **only replicas on that node** | preserved |
| **`Local` + ECMP** | ✓ per-flow across advertising nodes | ✓ all replicas, weighted per *node* not per pod | preserved |

The counterintuitive row is **`Local` + no ECMP**: the load balancing you get is
**pod-level, confined to one node**. Replicas elsewhere receive **zero** external
traffic — not lightly loaded, unused for ingress (they still serve in-cluster
traffic normally).

So `Cluster` makes ECMP a throughput optimization; `Local` makes it the thing
that lets other nodes' replicas serve at all. Per the
[Calico docs](https://docs.tigera.io/calico/latest/networking/configuring/advertise-service-ips),
Cluster gives *"good overall load balancing"*, Local is *"uneven"* — inherent,
since ECMP hashes the 5-tuple across nodes with no weighting by pod count.

### `Cluster` does not preserve the client IP — and L7 can't recover it

Calico is explicit: Cluster-mode traffic is *"load balanced across all nodes
using ECMP, then forwarded to the appropriate pod via **SNAT**."*

The SNAT is load-bearing — the ingress node may forward to a pod on another
node, and the reply must return via the ingress node to match the client's
connection state. **This applies even when the chosen pod is local**, because
kube-proxy marks externally-originated traffic for masquerade at the *service*
level, not per endpoint.

**The trap for our stack:** an ingress gateway (NGINX Gateway Fabric) behind a
`Cluster` LoadBalancer sees **node IPs as its clients**. Every `X-Forwarded-For`
it stamps records the node, not the caller — and you can't fix that at L7,
because the component that would add the header has already lost the
information.

### ✅ Use `Local` on the ingress Gateway from the start

At one all-in-one node, `Local` is **strictly better** — both of its usual
downsides are structurally impossible:

| | `Cluster` today | `Local` today |
|---|---|---|
| Source IP | lost to SNAT | **preserved** |
| Extra hop | yes | no |
| Uneven distribution | — | can't occur (one node) |
| Dropped if no local pod | — | can't occur (one node) |

At node 2 this becomes **active/standby ingress**: the best-path winner serves
everything, the other node's gateway replicas idle, and BGP failover promotes
the standby if the winner dies. That's a legitimate homelab posture — and with
`maximum-paths 8` already in the config, going active/active needs no pfSense
change at all.

### ⚠ BFD is not available to us

Fast failure detection is the obvious answer to slow failover, and pfSense's
neighbor config supports BFD. **It isn't an option** — BFD needs two
participants, and **open-source Calico doesn't implement it**. The
[Calico 3.32 resource list](https://docs.tigera.io/calico/latest/reference/resources/overview)
is `BGPConfiguration, BGPPeer, FelixConfiguration, GlobalNetworkPolicy,
GlobalNetworkSet, HostEndpoint, IPPool, NetworkPolicy, NetworkSet, Node,
Profile, WorkloadEndpoint` — no `BFDConfiguration`. That's Calico
Cloud/Enterprise only.

Hence `timers bgp 3 9` (§4). Note `bgp fast-external-failover` only helps on
genuine link-down; our nodes are Proxmox VMs, so one can die while pfSense's
switch port stays up and no link event fires. The hold timer is the real
detector.

### Other multi-node notes

- **Flow rehash on topology change.** FreeBSD's ECMP isn't consistent hashing,
  so adding or removing a node reshuffles the nexthop group and can break
  established connections, not merely redistribute new ones.
- **`Local` over BGP needs verification.** The historical "advertises from all
  nodes regardless of `Local`" bug
  ([#6074](https://github.com/projectcalico/calico/issues/6074)) is **fixed** —
  closed via PR #6282, affecting 3.21.2, long before our 3.32.1 pin. But a
  [recent 3.30.x discussion](https://github.com/orgs/projectcalico/discussions/10537)
  shows Local-over-BGP still needs deliberate configuration in some topologies
  (there, an LB IP bound to `eth0` was re-exported as a connected route, fixed
  with `ignoredInterfaces`). Verify advertisement comes only from
  endpoint-bearing nodes rather than assuming.
- **Kernel ECMP is fine.** pfSense ships `ROUTE_MPATH` with 5-tuple flow hashing
  (protocol, src/dst address, src/dst port), working since CE 2.7.0. The gap was
  only ever FRR's GUI, which §4 routes around.

---

## Sources

- [FRR Package](https://docs.netgate.com/pfsense/en/latest/packages/frr/index.html) · [Global Settings](https://docs.netgate.com/pfsense/en/latest/packages/frr/global/configuration.html) · [Status](https://docs.netgate.com/pfsense/en/latest/packages/frr/global/status.html) — Netgate
- [BGP Tab](https://docs.netgate.com/pfsense/en/latest/packages/frr/bgp/config-bgp.html) ("Master enable switch for BGP routing") · [Neighbors](https://docs.netgate.com/pfsense/en/latest/packages/frr/bgp/config-neighbor.html) · [Advanced](https://docs.netgate.com/pfsense/en/latest/packages/frr/bgp/config-advanced.html) — Netgate
- [Raw FRR Configurations](https://docs.netgate.com/pfsense/en/latest/packages/frr/raw/index.html) — Netgate
- [Multi-Path Routing](https://docs.netgate.com/pfsense/en/latest/routing/multipath.html) — Netgate (`ROUTE_MPATH`, 5-tuple hashing, FRR-only)
- [FRR BGP documentation](https://docs.frrouting.org/en/latest/bgp.html) — `maximum-paths`, `multipath-relax`, RFC 8212
- [Feature #16278](https://redmine.pfsense.org/issues/16278) — `maximum-paths` absent from the FRR GUI (open)
- [Todo #15785](https://redmine.pfsense.org/issues/15785) — FRR 10 targets CE 2.9.0 · [Feature #13575](https://redmine.pfsense.org/issues/13575) — FRR 9.0.1
- [Bug #7859](https://redmine.pfsense.org/issues/7859) — raw config silently ignored · [Bug #12928](https://redmine.pfsense.org/issues/12928) — vtysh saves invalidate GUI changes
- [opnsense/plugins#4015](https://github.com/opnsense/plugins/issues/4015) · [#4713](https://github.com/opnsense/plugins/issues/4713) — BGP listen range unimplemented
- [Calico: advertise service IPs](https://docs.tigera.io/calico/latest/networking/configuring/advertise-service-ips) · [resource overview](https://docs.tigera.io/calico/latest/reference/resources/overview) · [BGPPeer](https://docs.tigera.io/calico/latest/reference/resources/bgppeer) · [BGPConfiguration](https://docs.tigera.io/calico/latest/reference/resources/bgpconfig)
- [projectcalico#6074](https://github.com/projectcalico/calico/issues/6074) (fixed) · [discussion #10537](https://github.com/orgs/projectcalico/discussions/10537)
