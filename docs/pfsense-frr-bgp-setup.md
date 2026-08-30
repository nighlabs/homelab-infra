# Configuring FRR/BGP on pfSense CE 2.8.1-RELEASE

Peers pfSense with the k3s cluster so Calico can advertise **LoadBalancer
service IPs** into the LAN. The Calico side lives in `gitops/CLAUDE.md`
("Calico BGP — CRs, not Helm values"). The decisions this runbook implements,
with the alternatives rejected:

- raw config, not the GUI; explicit `neighbor` statements, not `bgp listen range`
  — [ADR-0022](decisions/0022-pfsense-frr-raw-config-explicit-neighbors.md)
- RFC 8212 satisfied by real prefix lists with `le 32`, never disabled —
  [ADR-0023](decisions/0023-rfc8212-real-policy-le32.md)
- the values: ASN and LB range derived from the cluster `index`, the LB range
  routed-only — [ADR-0026](decisions/0026-per-cluster-derivation-from-index.md)

**The config is generated** by `ansible/playbooks/render-frr-config.yml` from
`inventory/nodes.yml` + BWS — don't hand-write or GUI-edit it. See §9. It was
applied and verified live on 2026-08-16 (`worklog.md`).

> **⚠ This runs on live production gear.** The same pfSense box routes
> everything else in the house. Every step below is additive and parked —
> nothing changes existing routing until a Calico node actually peers. Read §8
> before starting.

**Blinding rule:** this document is committed, so it contains **no real
addresses or ASNs**. Values appear as `${placeholder}` matching the BWS secret
names (`ansible/BWS-SECRETS.md`). Do not paste real values back into this file.

---

## 1. Values

**Most are derived rather than chosen** — a cluster declares one number and
everything else falls out of it ([ADR-0026](decisions/0026-per-cluster-derivation-from-index.md)).

### Per-cluster values derive from `index`

Each cluster in `ansible/inventory/nodes.yml` carries an `index:`; its ASN and
its LoadBalancer range both derive from that one number, the same way every
host-shaped fact derives from `node_number`:

| Value | Derivation | `homelab` (index 1) |
|---|---|---|
| Cluster ASN | `bgp_asn_base + index` | `64601` |
| LoadBalancer range | `${lb_range_base}.<index>.0/24` | index `1` of that supernet |

Adding a cluster means adding an `index:` — not editing four variables and
hoping they stay consistent.

### Fixed values

| Value | Where | Note |
|---|---|---|
| pfSense ASN | `bgp_peer_asn: 64512` (cleartext) | **One** AS for the whole router, shared by every cluster's peering — it's one device, one AS |
| ASN base | `bgp_asn_base: 64600` (cleartext) | Leaves 64512–64599 free; keeps cluster ASNs visually distinct from pfSense's |
| pfSense peer IP | `bgp_peer_ip: {{ dmz_network.gateway }}` | **Not a new variable** — see below |
| LB supernet base | `lb_range_base` (BWS) | Environment-revealing, so never in Git |
| FRR master password | `frr_master_password` (BWS) | A **credential** — never in a committed manifest |

ASNs stay in cleartext deliberately: a number from the RFC 6996 private range
reveals nothing about the environment. Addresses stay in BWS.

**The peer IP is the DMZ gateway.** pfSense's BGP address *is* its DMZ interface
address, which *is* the nodes' default gateway — one value, already in BWS, so
there is no second variable to drift. ⚠ That equivalence breaks under **CARP**:
the gateway would be a VIP while BGP must peer with the physical interface
address. There is no CARP on that interface today (confirmed 2026-08-16); if
that changes, `bgp_peer_ip` becomes its own BWS secret.

### ⚠ The LB range must be routed-only

The LB range is **not** a subnet of the DMZ, and must be assigned to **no
interface anywhere** — not a pfSense interface, not a VLAN, not a DHCP pool. It
exists only as a BGP-learned route.

This was wrong in an earlier revision of this document, which called for "a /24
inside the DMZ subnet." That fails two ways, and neither is obvious:

- pfSense has a **connected** route for the DMZ. A BGP route of equal length
  loses to it on administrative distance, so pfSense forwards onto the DMZ
  segment instead of to a node.
- On-subnet clients skip routing entirely and **ARP** for the LB address.
  Nothing answers — Calico does no L2 for LoadBalancer IPs. That's MetalLB's
  L2 mode, which we don't run.

Using a range that exists nowhere else also makes the old "outside the DHCP
pool" constraint moot: there is no DHCP there to collide with.

`playbooks/render-frr-config.yml` asserts the derived ranges don't overlap the
DMZ subnet, the Ceph public network, or the pod/service CIDRs — but it cannot
see your pfSense interface list, so **that** part is on you.

### Two ASNs, and where they actually get set

Two distinct ASNs makes each session **eBGP**, which is what we want: it's the
mode with AS_PATH loop prevention, and the mode FRR's policy requirement applies
to (§4). iBGP would need a route reflector or full mesh to distribute anything,
which is pointless for a two-party peering.

**Every k3s node in a cluster shares that cluster's one ASN.** There is no
per-node AS assignment anywhere — the numbers are each stated twice, once per
side, and that's the complete list:

| # | Where | Setting |
|---|---|---|
| 1 | pfSense | `router bgp 64512` |
| 2 | pfSense | `neighbor <cluster> remote-as <cluster ASN>` (on the peer group) |
| 3 | Calico | `BGPConfiguration.spec.asNumber` = the cluster ASN |
| 4 | Calico | `BGPPeer.spec.asNumber` = `64512` |

On pfSense, nodes appear as explicit `neighbor` lines but inherit the remote AS
and the filters from their cluster's peer group (§4). On the Calico side
`BGPConfiguration.spec.asNumber` is the cluster-wide default every node
inherits, and `BGPPeer` is global (no `nodeSelector`), so it needs no per-node
entry either.

This makes the in-cluster mesh **iBGP** (all nodes one AS, full mesh — exactly
what `nodeToNodeMeshEnabled` provides) and each pfSense session **eBGP**.

⚠ **Do not use the per-node ASN override** (`Node.spec.bgp.asNumber`). It exists
for AS-per-rack topologies; here it would turn the mesh into eBGP *and* force
`bgp bestpath as-path multipath-relax` on the pfSense side before ECMP would
work at all (§4).

⚠ **Calico's default `asNumber` is `64512`** — the same number we gave pfSense.
Set the cluster's ASN explicitly. If a cluster ever landed on 64512 the session
would be iBGP and behave differently; the render playbook asserts against this
specific collision because it's a live trap, not a theoretical one.

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
  BGP peer password. It's the `frr_master_password` BWS secret; it's a
  credential, so per the root `CLAUDE.md` it never lands in Git.
- **Default Router ID** — leave unset; §4 sets it per-protocol.

**Services > FRR BGP**, *BGP* tab:

- **Enable** — check. This starts `bgpd`.
- **Local AS** — `64512` (`bgp_peer_asn`)
- **Router ID** — the DMZ interface address (`bgp_peer_ip`, §1)

Leave OSPF and OSPF6 disabled — we only want BGP.

> The Local AS and Router ID above are **ignored** once raw config is saved (§4
> sets them). Fill them in anyway: it costs nothing, and it means the GUI state
> is coherent rather than nonsense if anyone ever clears the raw config to fall
> back.

---

## 4. The FRR configuration

**Services > FRR Global Settings**, *Raw Config* tab → **Saved frr.conf**.

### Why raw, and what it costs

| | GUI | Raw config |
|---|---|---|
| `maximum-paths` (ECMP) | ✗ | ✓ |
| Config generated from the node map | ✗ | ✓ — §9 |
| Config reviewable in Git | ✗ | ✓ |
| GUI routing config still applied | ✓ | **✗** |

**`maximum-paths` alone justifies this.** It is
[Feature #16278](https://redmine.pfsense.org/issues/16278), open and unassigned,
and it's the only thing providing node-level load distribution — without it one
node wins best-path and takes 100% of ingress. `vtysh` is not an alternative:
per that report, *"Manual CLI changes via vtysh work until the next GUI 'Apply,'
which overwrites them."*

> **Changed 2026-08-16 — we no longer use `bgp listen range`.** Dynamic
> neighbors were originally the headline reason for raw config: any node in the
> DMZ subnet would be admitted automatically, so adding a node needed zero
> pfSense changes. We now use **explicit `neighbor` statements** instead. Why,
> in order of weight:
>
> 1. **All clusters share the DMZ subnet** (`inventory/nodes.yml` — node_number
>    uniqueness is global precisely because of this). A listen range maps one
>    address prefix to one peer group, and a peer group carries exactly one
>    `remote-as` *and* one set of prefix lists. So every cluster on that subnet
>    would be forced onto **one ASN and one permitted LB range**, with each
>    cluster free to advertise the other's. Splitting them would mean carving
>    the /24 into per-cluster bands and constraining how node_numbers are
>    allocated forever after.
> 2. **The neighbor list and the §6 firewall alias become the same list**, both
>    rendered from `inventory/nodes.yml`, so they cannot drift.
> 3. Explicit neighbors are the tightest admission control available.
>
> The cost is one pfSense paste per **added** node — not per rebuild, since node
> IPs derive deterministically from `node_number`. With §6 of `ansible/CLAUDE.md`
> planning three workers and then stability, that's a small, bounded number.
>
> `remote-as external` / `auto` would technically admit multiple ASNs through
> one listen range, but the peer group still carries one prefix list — so you'd
> get the multiple ASNs *without* the isolation that was the reason to want
> them, and lose the AS check that makes a misconfigured cluster fail loudly.

**The cost of raw config is total and confirmed:** *"If you are using Raw-Config
to add commands, the GUI will not be able to control the configuration. You need
to delete Raw-Config and add the configuration via GUI only."* There is no
warning banner on the pages that quietly stop working.

That's an acceptable trade because we don't hand-maintain the result — it's
generated (§9). A generated, asserted, version-controlled config is strictly
more reviewable and reproducible than GUI forms, which are unversioned by
definition.

### The config

⚠ **Do not hand-write this.** It is generated from `inventory/nodes.yml` + the
vault — see §9 for the command. Below is the shape it produces, with addresses
blinded; the real render carries one `neighbor` pair per node and one peer group
per cluster.

```
frr defaults traditional
service integrated-vtysh-config
log syslog informational
password ${frr_master_password}
!
router bgp 64512
 bgp router-id ${bgp_peer_ip}
 bgp log-neighbor-changes
 timers bgp 3 9
 !
 ! --- cluster: homelab (index 1, AS 64601, LB ${lb_range}) ---
 neighbor homelab peer-group
 neighbor homelab remote-as 64601
 neighbor ${node_ip} peer-group homelab
 neighbor ${node_ip} description snoop-a2o
 !
 address-family ipv4 unicast
  neighbor homelab activate
  ! RFC 8212 is satisfied by HAVING policy — see below.
  neighbor homelab prefix-list HOMELAB-IN in
  neighbor homelab prefix-list HOMELAB-OUT out
  maximum-paths 8
 exit-address-family
!
! Inbound: accept ONLY this cluster's LoadBalancer range.
ip prefix-list HOMELAB-IN seq 10 permit ${lb_range} le 32
ip prefix-list HOMELAB-IN seq 20 deny any
!
! Outbound: advertise nothing to the cluster.
ip prefix-list HOMELAB-OUT seq 10 deny any
!
```

A second cluster appends its own peer group, `neighbor` lines, and `-IN`/`-OUT`
prefix lists, with its own ASN and LB range. Nothing about the first changes.

### Line-by-line rationale

**`timers bgp 3 9`** — keepalive 3s, hold 9s. The default 180s hold means up to
three minutes of blackholing on node failure. The **negotiated hold time is the
minimum of the two peers' values**, so setting it here governs the session
regardless of Calico's default — no cluster-side change needed. See §10 for why
BFD isn't the answer.

**One peer group per cluster.** The peer group is what carries `remote-as` *and*
the prefix lists, so it — not the neighbor lines — is what actually separates one
cluster from another on a shared subnet. Nodes join it by explicit `neighbor
<ip> peer-group <cluster>` and inherit both.

**`neighbor <ip> description <hostname>`** — costs nothing and makes
`show bgp neighbors` readable, so a session problem names a host instead of an
address you have to go look up.

**No `no bgp ebgp-requires-policy`.** FRR 7.4+ implements RFC 8212: an eBGP
session with no policy discards all routes in both directions while still
reporting `Established`. Most guides disable the requirement. We instead
*satisfy* it with the two prefix lists — we want the inbound filter anyway, and
once it exists, disabling the requirement only discards a safety net. **Both
directions must be populated**; an inbound filter alone still gets outbound
routes discarded.

**`<CLUSTER>-IN`** is the real security boundary. It guarantees pfSense never
installs a route to the pod CIDR (`10.42.0.0/16`) even if the Calico-side
`BGPFilter` is wrong or missing. The `BGPFilter` is enforced by the very device
we'd be guarding against misconfiguring, so it isn't trusted alone — these are
two independent controls.

**⚠ `le 32` is load-bearing, and its absence fails silently.** Without it FRR
matches the prefix length *exactly*: *"In the case of no le or ge command, the
prefix length must match exactly the length specified in the prefix list."*
Calico's advertisement granularity is not fixed — it follows the Service's
`externalTrafficPolicy`:

| Policy | What Calico advertises | Matched by bare `permit …/24`? |
|---|---|---|
| `Cluster` | the **whole block**, from every node | ✓ |
| `Local` | a **/32 per Service**, from nodes holding a backend | ✗ — **dropped** |

So a bare `permit ${lb_range}` works right up until the first `Local` Service,
which then establishes a perfectly healthy-looking session and blackholes. `le
32` covers both modes and costs nothing, so there's no reason to pick one and
bet on it.

> **✅ VERIFIED LIVE 2026-08-16 — both modes, on one cluster.** A `Cluster`
> Service produced only the `/24` block (no route of its own); a `Local` Service
> produced `x.x.x.130/32`, and pfSense **accepted it** — `show ip bgp` listed
> both. ⚠ Note the `/32` is redundant for *reachability* at a single node, so a
> broken filter here is invisible to `curl` and shows up only in the routing
> table. Test it with `show ip bgp`, never with a connectivity check.

**`<CLUSTER>-OUT` denies everything.** The k3s nodes get their default route
statically from Ignition (repo rule: no DHCP in cluster networking), so they
need to learn nothing from pfSense. This also means the LAN table can never leak
into the cluster.

**`maximum-paths 8`** — ECMP across nodes advertising the same LB prefix. Inert
at one node, and the sole reason raw config is required at all. See §10 for what
it does and doesn't buy.

**No `bgp bestpath as-path multipath-relax`** — deliberately. ECMP across eBGP
peers normally needs it, because without it the *entire* AS_PATH must match, not
just its length. Since every node in a cluster shares one ASN (§1), the AS_PATH
from all of them is byte-identical. Another reason to avoid per-node ASNs:
they'd require this too.

### ⚠ Verify the password line

The raw config replaces the generated `frr.conf` wholesale, so the master
password must be *in it*. Whether the package still injects it independently is
worth confirming on first apply rather than assuming — if `vtysh` works and the
daemons are healthy (§7), you're fine.

---

## 5. Apply it

### ⚠ First, read what's already there

Raw config replaces the running `frr.conf` **wholesale**, so anything the GUI
generator put there disappears. Before pasting, capture the current
`vtysh -c 'show running-config'` and check it for:

- **`network <prefix>` statements.** These make pfSense *originate* a prefix.
  That is backwards for this design — the cluster advertises the LB range **to**
  pfSense and pfSense learns it. A `network` statement means the prefix exists
  whether or not any node is up, so you blackhole instead of failing over.
  Delete it; don't carry it across.
- **`no bgp network import-check`.** Exists only to let a `network` statement
  advertise a prefix with no matching route. It goes with the `network` line.
- **Pre-existing prefix lists** (e.g. a `k8s_prefixlist` from an earlier
  attempt). The render emits `<CLUSTER>-IN`/`-OUT`; old lists simply vanish.
  Confirm nothing else references them first.
- **An existing LB range that isn't the one you configured.** Check firewall
  rules, static routes and DNS before retiring it.
- **`hostname` / `line vty`.** GUI-generator artifacts, purely cosmetic
  (`hostname` sets the vtysh prompt). They go; nothing depends on them.
- **`router bgp <ASN>` and `bgp router-id`** — these should already match
  `bgp_peer_asn` and `bgp_peer_ip` from §1. If the router-id differs, resolve
  that *before* pasting rather than letting the render silently move it.

A config with **no `neighbor` statements has no sessions**, so replacing it
disrupts nothing. That's the easy case — verify it with `show bgp summary`
before assuming.

### Then

0. Render it (§9):
   `ansible-playbook playbooks/render-frr-config.yml`
1. Paste **the whole of** `ansible/.frr/frr.conf` into **Saved frr.conf** and
   save. Always paste the entire file — it's generated, so a partial edit is
   silently reverted on the next render.
2. Restart FRR (**Status > Services**, or toggle Global Settings Enable).
3. Confirm it actually loaded — the GUI accepting the paste is not proof:

```sh
vtysh -c 'show running-config'
```

⚠ **It will NOT be byte-identical to what you pasted, and that's normal.** FRR
rebuilds the config from its internal state rather than echoing your text, so
**every `!` comment is stripped** — and the rendered file is mostly comments.
Lines may also be reordered and unstated defaults may appear. Compare *meaning*,
not text. Present and correct means:

- `router bgp` + `bgp router-id` match §1
- `timers bgp 3 9` and `maximum-paths 8`
- one `neighbor <cluster> peer-group` + `remote-as` per cluster
- one `neighbor <ip> peer-group <cluster>` per node in the map
- both `prefix-list … in` / `… out` bindings
- `ip prefix-list <CLUSTER>-IN … permit <lb_range> le 32` — **check the `le 32`
  survived**, it's the difference between working and silently blackholing
- **nothing left over** from a previous config — no `network` statement, no
  stale prefix list (see the pre-paste checklist above)

Two omissions are **normal**, verified on FRR 9.1.2:

- **`neighbor <cluster> activate` will not appear.** `frr defaults traditional`
  implies `bgp default ipv4-unicast`, so neighbors auto-activate in IPv4 unicast
  and FRR drops the redundant line. Confirm it took via
  `show bgp neighbors` → `For address family: IPv4 Unicast / <cluster>
  peer-group member`.
- **`hostname` reappears even though the template omits it** — FRR falls back to
  the system hostname.

⚠ **Redact the `password` line before pasting output anywhere.** It's the FRR
master password in cleartext: `vtysh -c 'show running-config' | grep -v password`.

If those aren't there, **stop**. [Bug #7859](https://redmine.pfsense.org/issues/7859)
was a case where a config-tag rename caused raw config to be *silently ignored*,
so this is a real failure mode, not a formality.

---

## 6. Firewall rule

**Firewall > Rules > [DMZ interface]**. BGP is TCP/179 and pfSense's own
listening services are not exempt from interface rules:

| Field | Value |
|---|---|
| **Action** | Pass |
| **Protocol** | TCP |
| **Source** | an alias holding the k3s node addresses |
| **Destination** | This Firewall (self) |
| **Destination Port** | 179 (BGP) |

**The alias members are generated** — `ansible/.frr/bgp-nodes.txt`, written by
the same play that renders `frr.conf`, from the same node map. That's deliberate:
the BGP neighbor list and the firewall alias are the same list of addresses, and
deriving both from `inventory/nodes.yml` is what stops them drifting apart. When
you add a node, both files change in one render.

Source-restrict it regardless. It's the outermost of the three controls bounding
who can peer — the others being the explicit `neighbor` lines (§4) and the peer
group's `remote-as` check.

**Verify the alias actually has members:**

```sh
pfctl -t <alias name> -T show     # must list every node IP from bgp-nodes.txt
```

⚠ **An empty alias and a correct one look identical in the rule counters.** Both
show evaluations with zero packets, because this rule governs the *node dialing
pfSense* — and pre-Calico nothing ever does. `pfctl -T show` is the only check
that distinguishes them.

Traffic in the other direction needs no rule: pfSense dialing out to a node on
179 is covered by the built-in *"let out anything from firewall host itself"*.
Seeing a state like `<peer ip>:<port> -> <node ip>:179` is good evidence that
routing and L2 reachability to the node are fine, independent of BGP.

Separately, once LB routes are being learned, traffic *to* the LB range from
other segments needs its own pass rules. Learning a route and being permitted to
use it are different things — a working BGP session plus a missing firewall rule
looks exactly like a broken BGP session from a client.

### ✅ Put the whole LB supernet in the "internal networks" alias

**Decided 2026-08-16.** Add the **`/16` supernet** (not the per-cluster `/24`) to
whatever alias means "internal networks" — here, `<HomeNets>`.

Isolated VLANs are typically written as `pass … to ! <HomeNets>` followed by a
catch-all `block drop`. Leaving the LB range *out* of that alias therefore grants
those segments **unrestricted access to every LoadBalancer service** — quietly,
via a rule written years earlier for internet access. Putting it in makes LB
reachability **fail closed**: anything that should reach a service needs an
explicit rule.

Use the supernet so every future cluster's `<base>.<index>.0/24` inherits the
policy automatically, instead of becoming a per-cluster checklist item.

Safe to apply: an alias used only in `to ! <alias>` tests can never grant
reachability by growing, only remove it.

**Return traffic is unaffected**, including where a rule blocks the cluster
segment from initiating to LAN. pf creates a **state pair** for routed flows —
one state per interface — so replies match the outbound state rather than being
re-evaluated against inbound block rules. Confirm on any existing cross-segment
flow in `pfctl -ss` before worrying about it.

---

## 7. Verification

**With explicit neighbors, a parked config lists every node up front**, sitting
in `Active` or `Connect` and retrying. That is **expected and harmless** before
the cluster exists — pfSense dials out to nodes that aren't up yet, and logs it.
It's also useful: you can confirm the intended peer list is right before there's
anything to peer with.

> Earlier revisions used `bgp listen range`, where the opposite held — a parked
> config showed *no* neighbors at all, and an empty `show bgp summary` was
> success. If you're reading an old note that says that, it no longer applies
> (§4).

> **Running these:** SSH to pfSense and pick **8) Shell** (or use
> **Diagnostics > Command Prompt**). `vtysh -c '<command>'` runs one and exits;
> bare `vtysh` gives an interactive prompt where `?` is context help and
> `terminal length 0` turns off `--More--` paging. ⚠ **Read-only** — vtysh can
> configure FRR, but those edits are wiped on the next GUI Apply (§4), and the
> rendered file is the source of truth.

Pre-cluster, verify the daemon is up, the config loaded, and the neighbor list
matches the node map:

```sh
# bgpd actually running (§3 — the silent-failure check)
vtysh -c 'show version'

# The config in force IS the config we pasted (§5)
vtysh -c 'show running-config'

# Listening at all
sockstat -4 -l | grep 179

# Every node from inventory/nodes.yml present, Active/Connect (not Established)
vtysh -c 'show bgp summary'
```

Post-cluster:

```sh
# Every node now Established
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
3. Pre-cluster: `show bgp summary` lists every node from `inventory/nodes.yml`
   in `Active`/`Connect` (expected — nothing is up yet).
4. Post-cluster: every k3s node is `Established`, in its own cluster's peer
   group.
5. `show ip bgp` contains the LB range — **and no pod CIDR**. If `10.42.0.0/16`
   appears, *both* the Calico `BGPFilter` and `<CLUSTER>-IN` failed; stop and
   fix.
6. `advertised-routes` is empty for every neighbor.
7. **Reach an actual LoadBalancer IP from another segment.** RFC 8212 refusal
   and a missing `le 32` both present as a healthy `Established` session with
   nothing flowing, so the session state is not the test — the reachability is.

### 🔁 Re-verify after every FRR package update

Raw config is more fragile across upgrades than GUI config, precisely because
the package isn't regenerating it. Re-run steps 1–2 after any FRR package or
pfSense upgrade rather than assuming it survived.

---

## 8. Blast radius and rollback

Why this is safe to stage ahead of the cluster:

- **Nothing is redistributed.** Every `<CLUSTER>-OUT` denies everything and no
  `network` statements exist, so pfSense advertises no routes and no existing
  routing decision changes.
- **No session, no routes.** Until a Calico node connects, the config is inert —
  the `neighbor` lines just retry into nothing.
- **Learned routes are filtered to one `/24` per cluster**, each of which is
  otherwise unused and attached to no interface (§1).

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

### The config is generated, not hand-written

```sh
cd ansible
ansible-playbook playbooks/render-frr-config.yml
```

Writes two git-ignored files to `ansible/.frr/`:

| File | Goes where |
|---|---|
| `frr.conf` | §5 — Services > FRR Global Settings > Raw Config |
| `bgp-nodes.txt` | §6 — members of the firewall alias |

Both are rendered from `inventory/nodes.yml` + BWS, so the BGP neighbor
list and the firewall alias cannot disagree. Same Jinja2-from-secrets pattern
the repo already uses for Ignition.

The play reads inventory and writes two local files — it does **not** touch
pfSense or any node. Delivery is a manual paste, because pfSense CE ships no API.

> ⚠ **pfSense is a render target, not a source of truth.** Editing the raw
> config directly is tempting for a one-line change, and it works — until the
> next render, which regenerates the whole file from BWS and reverts your
> edit on paste. **Silently**: nothing errors, the value just goes backwards.
>
> The worst case is a rotated credential. Change the FRR master password on
> the box but not in BWS, and the next paste quietly restores the old
> password. Whatever you change on pfSense, change in BWS too — even if you
> don't re-render right away.
>
> Before any paste, diff the new render against what's running and confirm the
> only differences are ones you intended.

**Committed:** `playbooks/templates/frr.conf.j2`, `playbooks/render-frr-config.yml`.
**Never committed:** anything under `ansible/.frr/` — `frr.conf` embeds the FRR
master password, so it's written `0600` into a `0700` directory and ignored in
`ansible/.gitignore`. To share or diff a render safely:

```sh
ansible-playbook playbooks/render-frr-config.yml \
  -e frr_redact_password=true
```

The rendered file opens with a banner stating the GUI is inert and edits belong
in the template. That's the failure mode most likely to bite a future reader —
including us.

**What the play asserts before rendering** (it fails rather than emitting a
config that would break something):

- every cluster declares an `index`, unique, in 1..254
- cluster ASNs are in 64512–65534, unique, and **different from pfSense's** —
  the Calico-default-`64512` trap in §1
- no derived LB range overlaps the DMZ subnet, the Ceph public network, or the
  pod/service CIDRs

It cannot see your pfSense interface list, so the "assigned to no interface"
half of §1 stays a manual check.

⚠ The play derives each neighbor address as
`{{ dmz_network.subnet_base }}.{{ node_number }}` — the **same** derivation as
`roles/flatcar_vm/tasks/preflight.yml`'s `eth0_ip`, duplicated in the one other
place that needs it. If that derivation ever changes, change it in both, or
pfSense will peer with addresses no node holds.

### BWS secrets

| Secret | Value |
|---|---|
| `lb_range_base` | first two octets of the LB supernet |
| `frr_master_password` | the FRR master password |

Only these two exist for the BGP work. The pfSense peer IP is
`dmz_network.gateway` (the `dmz_gateway` secret) and both ASNs are cleartext
constants in `group_vars/all/vars.yml` — see §1. Full manifest:
`ansible/BWS-SECRETS.md`.

The address-shaped values flow to the cluster as the `cluster-topology` Secret,
consumed via Flux `postBuild.substituteFrom` — so committed manifests keep
`${bgp_peer_ip}` / `${lb_range}` placeholders and nothing environment-revealing
lands in Git. See `gitops/CLAUDE.md` ("Topology blinding"). The ASNs need no
blinding and can appear literally.

`frr_master_password` is a **credential**, not topology — it never reaches a
committed manifest.

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

> **⚠ This matrix is historical.** It assumes the standard iptables/kube-proxy
> dataplane, which we no longer run. The cluster is on **Calico's eBPF dataplane
> with kube-proxy disabled entirely**, verified on a from-scratch rebuild
> ([ADR-0024](decisions/0024-calico-ebpf-dataplane-no-kube-proxy.md)), and eBPF
> **preserves the source IP under `Cluster`**. That collapses all four rows into one:
>
> | | Node-level | Pod-level | Source IP |
> |---|---|---|---|
> | **`Cluster` + ECMP (what we run)** | ✓ per-flow across nodes | ✓ all replicas | **preserved** |
>
> There is no `Local`-vs-`Cluster` trade to make. The rows below are kept because
> they explain *why* the trade used to exist, and they'd apply again if the eBPF
> dataplane were ever reverted.
>
> **Nothing in this runbook changed** — eBPF replaces kube-proxy, not routing, so
> the `frr.conf` in §4 is identical either way. The one place it does matter is
> `le 32` in the prefix list (§4): policy choice drives whether Calico advertises
> the whole block or /32s, and `le 32` is what makes the filter correct under
> both.

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

> **⚠ Historical — does not apply to the eBPF dataplane we run.** This section
> describes kube-proxy's behavior, which was the reason to prefer `Local`. Under
> Calico eBPF the SNAT below doesn't happen and the client IP survives `Cluster`
> (ADR-0024). Kept because it explains the trade, and because it applies again
> if eBPF is ever reverted.

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

### ✅ Use `Cluster` on the ingress Gateway — superseded 2026-08-03

> **This section previously said "use `Local` on the ingress Gateway from the
> start."** That advice was correct for the iptables dataplane and is now wrong.
> Its entire justification was source-IP preservation, which `Cluster` now gives
> us for free.

With the eBPF dataplane, `Cluster` wins on every axis that used to favor
`Local`:

| | `Cluster` (eBPF) | `Local` |
|---|---|---|
| Source IP | **preserved** | preserved |
| Pod-level spread | **all replicas, cluster-wide** | only replicas on the ingress node |
| Node-level spread | per-flow across all nodes | per-flow across *advertising* nodes only |
| Dropped if no local pod | can't happen | possible once there's more than one node |
| Advertised prefix | whole block, one route | a /32 per Service |

The old `Local` recommendation also carried a second-node consequence —
active/standby ingress, with the best-path winner serving everything and the
other node's replicas idle. `Cluster` doesn't have that shape: every node
advertises, and `maximum-paths 8` spreads across all of them from the moment the
second one joins.

**Use `Local` only for a specific Service that genuinely needs traffic pinned to
nodes holding a backend** — not as the default. And note that doing so changes
what gets advertised (a /32, not the block), which is exactly the case `le 32`
in §4 exists to keep working.

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
- [opnsense/plugins#4015](https://github.com/opnsense/plugins/issues/4015) · [#4713](https://github.com/opnsense/plugins/issues/4713) — BGP listen range unimplemented in the GUI (context only; we no longer use listen ranges — §4)
- [FRR: prefix lists](https://docs.frrouting.org/en/latest/filter.html) — `le`/`ge`; without them the prefix length must match exactly (§4)
- [FRR: BGP](https://docs.frrouting.org/en/latest/bgp.html) — peer groups, `remote-as internal|external|auto`
- [Calico: advertise service IPs](https://docs.tigera.io/calico/latest/networking/configuring/advertise-service-ips) · [resource overview](https://docs.tigera.io/calico/latest/reference/resources/overview) · [BGPPeer](https://docs.tigera.io/calico/latest/reference/resources/bgppeer) · [BGPConfiguration](https://docs.tigera.io/calico/latest/reference/resources/bgpconfig)
- [projectcalico#6074](https://github.com/projectcalico/calico/issues/6074) (fixed) · [discussion #10537](https://github.com/orgs/projectcalico/discussions/10537)
