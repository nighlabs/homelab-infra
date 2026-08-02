# Configuring FRR/BGP on pfSense CE 2.8.1-RELEASE

Peers pfSense with the k3s cluster so Calico can advertise **LoadBalancer
service IPs** into the LAN. Companion to `ansible/CLAUDE.md` §7 items 6 + 8;
the Calico side lives in `gitops/CLAUDE.md` ("Calico BGP — CRs, not Helm
values").

> **⚠ This runs on live production gear.** The same pfSense box routes
> everything else in the house. Every step below is additive and parked —
> nothing changes existing routing until a Calico node actually peers. Read
> "Blast radius" before starting.

**Blinding rule:** this document is committed, so it contains **no real
addresses or ASNs**. Values appear as `${placeholder}` matching the vault
variable names. Fill them in from `ansible/inventory/group_vars/all/vault.yml`
as you go. Do not paste real values back into this file.

---

## 1. Values to decide first

These four are the open blockers from `ansible/CLAUDE.md` §7 item 6. Decide all
four before touching the GUI — they're inputs to both sides of the peering, and
a mismatch just yields a session that never establishes.

| Value | Vault variable | Constraint |
|---|---|---|
| Cluster ASN | `vault_calico_asn` | Private range **64512–65534** |
| pfSense ASN | `vault_bgp_peer_asn` | Private range, **different** from the cluster ASN |
| pfSense peer IP | `vault_bgp_peer_ip` | pfSense's **DMZ interface** address |
| LoadBalancer range | `vault_lb_range` | A `/24` inside the DMZ subnet, **outside** the DHCP pool and outside any static assignments |

Two distinct ASNs makes this **eBGP**, which is what we want: eBGP decrements
TTL, applies loop prevention via AS_PATH, and — critically — is the mode where
FRR enforces the policy requirement covered in §4. iBGP (same ASN both sides)
would need a route reflector or full mesh to distribute anything, which is
pointless for a two-party peering.

The LB range must not overlap anything pfSense already hands out. Calico owns
these addresses via ARP-less BGP advertisement; if DHCP also leases one, you get
an intermittent duplicate-address failure that looks like a BGP problem and
isn't.

---

## 2. Confirm the FRR package version

pfSense CE **2.8.1** predates the FRR 10 package. Per
[Todo #15785](https://redmine.pfsense.org/issues/15785), FRR 10 targets pfSense
CE **2.9.0** / Plus 25.11 — so on 2.8.1 you are on the earlier package branch
(the FRR 9.x line;
[#13575](https://redmine.pfsense.org/issues/13575) moved it to 9.0.1).

Install via **System > Package Manager > Available Packages**, search `frr`.

Then confirm what you actually got, because everything in §4 depends on it being
FRR ≥ 7.4:

**Diagnostics > Command Prompt**, or SSH:

```sh
pkg info | grep -i frr
vtysh -c 'show version'
```

Package versions move independently of the pfSense base release, so read the
output rather than assuming. Anything in the 8.x / 9.x / 10.x range behaves
identically for our purposes.

---

## 3. Global settings

**Services > FRR Global Settings**, *Global Settings* tab:

- **Enable FRR** — check.
- **Master Password** — required; FRR will not start without it. This is the
  vtysh/daemon password, not a BGP peer password. Store it in the vault
  (`vault_frr_master_password`) — it is a credential, so per the root
  `CLAUDE.md` it belongs in BWS, never in Git.
- **Default Router ID** — leave unset here; set it per-protocol in §4 so BGP's
  identity is explicit rather than inherited.

Leave the other daemons (OSPF, OSPF6) disabled. We only want BGP.

---

## 4. BGP router configuration

**Services > FRR BGP**, *BGP* tab:

| Field | Value |
|---|---|
| **Enable** | checked |
| **Local AS** | `${vault_bgp_peer_asn}` — pfSense's own ASN |
| **Router ID** | `${vault_bgp_peer_ip}` — pfSense's DMZ address |
| **Log Adjacency Changes** | checked |
| **Networks to Distribute** | **leave empty** |

Two of these are worth dwelling on.

**Local AS is pfSense's ASN, not the cluster's.** Easy to transpose, and the
symptom is unhelpful — the session sits in `Active`/`Connect` and the log says
little beyond a notification about a bad peer AS.

**Networks to Distribute stays empty on purpose.** The k3s nodes get their
default route statically from Ignition (repo rule: no DHCP anywhere in cluster
networking), so they need to learn *nothing* from pfSense. Leaving this empty
means the session is effectively one-directional: Calico advertises LB routes
up, pfSense advertises nothing down. Less state, and no chance of leaking the
LAN table into the cluster. §6 enforces that with an explicit filter rather than
relying on this field staying empty.

### The eBGP policy requirement

FRR 7.4+ implements **RFC 8212**: an eBGP session with no inbound/outbound
policy discards *all* routes in both directions. The session establishes
normally and shows `Established`, and no prefixes move. This is the single most
common way this setup silently fails.

There are two valid ways to satisfy it, and the repo notes in §7 item 8 name
only the first:

**Option A — disable the requirement.** **Services > FRR BGP > Advanced**, check
**"Disable eBGP Require Policy"**. One click, and it's what most MetalLB-era
guides tell you to do.

**Option B — apply real policy (recommended here).** Configure the prefix lists
in §6 and RFC 8212 is satisfied *because policy exists*. Leave "Disable eBGP
Require Policy" **unchecked**.

Option B is worth the extra ten minutes. We want an inbound filter on pfSense
anyway as defense-in-depth — the Calico-side `BGPFilter` is the primary control
that stops the pod CIDR from leaking, but it's enforced by the device we'd be
trying to protect ourselves from misconfiguring. A pfSense-side prefix list is
an independent check. Once it exists, disabling the requirement buys nothing and
costs a safety net.

Use Option A only to isolate a fault during bring-up, then go back to B.

---

## 5. Neighbors — one entry per k3s node

> **Correction to earlier guidance:** `bgp listen range` (dynamic neighbors) is
> **not exposed in the pfSense FRR GUI**. It's a longstanding gap — the
> equivalent OPNsense requests
> ([#4015](https://github.com/opnsense/plugins/issues/4015),
> [#4713](https://github.com/opnsense/plugins/issues/4713)) are still open, and
> operators report hand-editing the config file to get it. The Raw Config
> workaround is a trap here — see §8. **Use explicit neighbors.**

The peer-group mechanism still does most of the work, so adding a node stays a
small repeatable step rather than a re-derivation.

### 5a. Create the peer group

**Services > FRR BGP**, *Peer Groups* tab. Add:

| Field | Value |
|---|---|
| **Name** | `k3s-nodes` |
| **Description** | `k3s cluster — Calico BGP` |
| **Remote AS** | `${vault_calico_asn}` |
| **Update Source** | pfSense's DMZ interface |
| **Address Family** | IPv4 |

Attach the §6 filters here, not per-neighbor.

### 5b. Add one neighbor per node

**Services > FRR BGP**, *Neighbors* tab. For each k3s node:

| Field | Value |
|---|---|
| **Name/Address** | the node's DMZ IP |
| **Description** | the node hostname |
| **Peer Group** | `k3s-nodes` |

Remote AS and filters come from the group. Today that's one entry (`snoop-a2o`,
all-in-one). Each node added later gets one row here **and** a corresponding
`BGPPeer` on the Calico side.

Do **not** set eBGP Multihop — nodes are on the same L2 segment as the pfSense
DMZ interface, so the default TTL of 1 is correct. Setting it would mask a
misrouted peering rather than fix one.

**Password** is optional. TCP-MD5 between two devices you control on a segment
you control adds a credential to manage for little gain; skip it unless the DMZ
is genuinely untrusted. If you do set it, it must match `BGPPeer.spec.password`
(a Secret reference) on the Calico side, and it becomes a BWS-managed credential.

---

## 6. Prefix lists — the actual security boundary

**Services > FRR Global/Zebra**, *Prefix Lists* tab.

### Inbound: accept only the LB range

| Field | Value |
|---|---|
| **Name** | `K3S-IN` |
| **Sequence** | `10` |
| **Action** | permit |
| **Network** | `${vault_lb_range}` |

Add a second entry, sequence `20`, action **deny**, network `0.0.0.0/0` with
`le 32`, to make the reject explicit rather than relying on the implicit
deny-all.

This is what guarantees pfSense never installs a route to the pod CIDR
(`10.42.0.0/16`), even if the Calico-side `BGPFilter` is wrong or absent. Two
independent controls, and neither one alone is trusted.

### Outbound: advertise nothing

| Field | Value |
|---|---|
| **Name** | `K3S-OUT` |
| **Sequence** | `10` |
| **Action** | deny |
| **Network** | `0.0.0.0/0` with `le 32` |

Belt and braces with the empty **Networks to Distribute** in §4.

### Attach them

Back on the `k3s-nodes` peer group, under **Prefix List Filter**:

- **Inbound** → `K3S-IN`
- **Outbound** → `K3S-OUT`

Both directions must be populated for RFC 8212 to be satisfied under Option B.
An inbound filter alone still gets outbound routes discarded.

---

## 7. Firewall rule

**Firewall > Rules > [DMZ interface]**. BGP is TCP/179 and pfSense's own
listening services are not exempt from interface rules:

| Field | Value |
|---|---|
| **Action** | Pass |
| **Protocol** | TCP |
| **Source** | the k3s node addresses (an alias is tidiest) |
| **Destination** | This Firewall (self) |
| **Destination Port** | 179 (BGP) |

Source-restrict it to the nodes. An any-source rule on 179 invites anything on
the DMZ to attempt a session.

Separately, once LB routes are being learned, traffic *to* the LB range from
other segments needs its own pass rules. Learning a route and being permitted to
use it are different things — a working BGP session plus a missing firewall rule
looks exactly like a broken BGP session from a client.

---

## 8. ⚠ Do not use the Raw Config tab

**Services > FRR Global Settings**, *Raw Config* tab exists and will tempt you
into hand-writing `bgp listen range`. Don't.

Per the [Netgate documentation](https://docs.netgate.com/pfsense/en/latest/packages/frr/raw/index.html):
saving a raw config means **the GUI configuration is no longer applied at all**
until the raw field is cleared. It is not a supplement to the GUI — it fully
replaces it.

The consequences are worse than the inconvenience:

- Every future GUI change silently does nothing. There is no warning banner on
  the pages that stop working.
- The FRR config becomes invisible to Ansible and to this document — a
  hand-edited box is a box nobody else can reason about.
- Related: with `vtysh`-side saves, GUI changes are similarly invalidated
  ([Bug #12928](https://redmine.pfsense.org/issues/12928)).

Explicit neighbors are mildly tedious at cluster-growth time. That is a much
better trade than an un-managed firewall config. Revisit if pfSense 2.9.0's FRR
10 package exposes listen ranges.

---

## 9. Verification

Expect **`Active` or `Connect` with no session** until Calico is peering. That
is success for a parked config, not a fault. Resist the urge to "fix" it.

**Status > FRR**, *BGP* tab — or from the shell:

```sh
# Neighbor state — parked: Active/Connect. Peered: Established.
vtysh -c 'show bgp summary'

# Full neighbor detail, incl. why a session is not up
vtysh -c 'show bgp neighbors ${node_ip}'

# What we have LEARNED — must contain the LB range and nothing else
vtysh -c 'show ip bgp'

# What we are ADVERTISING — must be empty
vtysh -c 'show ip bgp neighbors ${node_ip} advertised-routes'

# Confirm the daemon is listening at all
sockstat -4 -l | grep 179
```

Verify the config that's actually running, which is not necessarily the one in
the GUI if §8 was ignored:

```sh
vtysh -c 'show running-config'
```

### Acceptance criteria

Before calling this done:

1. `show bgp summary` lists every k3s node as a neighbor.
2. Sessions are `Active`/`Connect` (pre-cluster) or `Established` (post-cluster).
3. `show ip bgp` contains the LB range — **and no pod CIDR**. If `10.42.0.0/16`
   appears, both the Calico `BGPFilter` and `K3S-IN` failed; stop and fix before
   proceeding.
4. `advertised-routes` is empty for every neighbor.
5. TCP/179 is listening and the firewall rule passes from node addresses only.

---

## 10. Blast radius and rollback

Why this is safe to stage ahead of the cluster:

- **Nothing is redistributed.** Empty Networks to Distribute + `K3S-OUT` deny-all
  means pfSense advertises no routes, so no existing routing decision changes.
- **No session, no routes.** Until a Calico node connects, the config is inert.
- **Learned routes are filtered to one `/24`** that is otherwise unused.

The one genuinely global change is **Enable FRR** in §3, which starts the
daemons. On a box that has never run FRR that's a new listening service, not a
change to existing forwarding.

**Rollback:** uncheck **Enable FRR** (§3). Routing reverts to static/kernel
immediately. The neighbor and prefix-list config persists harmlessly for a
retry.

**Take a config backup first** — **Diagnostics > Backup & Restore** — so a
restore is a known-good path rather than an undo-by-memory.

---

## 11. Feeding the values back into the repo

Once decided, add to `ansible/inventory/group_vars/all/vault.yml`:

```yaml
vault_calico_asn: <cluster ASN>
vault_bgp_peer_asn: <pfSense ASN>
vault_bgp_peer_ip: "<pfSense DMZ address>"
vault_lb_range: "<LB /24>"
vault_frr_master_password: "<FRR master password>"
```

These flow to the cluster as the `cluster-topology` Secret, consumed via Flux
`postBuild.substituteFrom` — so the committed manifests keep `${bgp_peer_ip}` /
`${bgp_peer_asn}` placeholders and nothing environment-revealing lands in Git.
See `gitops/CLAUDE.md` ("Topology blinding").

`vault_frr_master_password` is a **credential**, not topology — it belongs in
BWS and never reaches a committed manifest.

Remember the strict-substitution gate: an undefined `${var}` becomes an empty
string and reconciles **successfully**, so a typo yields `peerIP: ""` reported
healthy.

---

## Sources

- [BGP Neighbor Configuration](https://docs.netgate.com/pfsense/en/latest/packages/frr/bgp/config-neighbor.html) — Netgate
- [BGP Tab Configuration](https://docs.netgate.com/pfsense/en/latest/packages/frr/bgp/config-bgp.html) — Netgate
- [Advanced BGP Configuration](https://docs.netgate.com/pfsense/en/latest/packages/frr/bgp/config-advanced.html) — Netgate
- [BGP Example Configuration](https://docs.netgate.com/pfsense/en/latest/packages/frr/bgp/example.html) — Netgate
- [Raw FRR Configurations](https://docs.netgate.com/pfsense/en/latest/packages/frr/raw/index.html) — Netgate
- [FRR Status](https://docs.netgate.com/pfsense/en/latest/packages/frr/global/status.html) — Netgate
- [Todo #15785 — update to FRR 10](https://redmine.pfsense.org/issues/15785) (targets CE 2.9.0)
- [Feature #13575 — update to FRR 9.0.1](https://redmine.pfsense.org/issues/13575)
- [Bug #12928 — vtysh saves invalidate GUI changes](https://redmine.pfsense.org/issues/12928)
- [opnsense/plugins#4015](https://github.com/opnsense/plugins/issues/4015),
  [#4713](https://github.com/opnsense/plugins/issues/4713) — BGP listen range still unimplemented
