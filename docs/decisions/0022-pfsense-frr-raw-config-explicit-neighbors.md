# ADR-0022: pfSense FRR is managed as generated raw config (not the GUI), with explicit `neighbor` statements (not `bgp listen range`)

- **Date:** 2026-08-02 (raw config decided) · 2026-08-16 (revised to explicit neighbors; config generator written; verified live)
- **Status:** Accepted
- **Supersedes / related:** [ADR-0018](0018-calico-bgp-replaces-metallb.md) (what peers with it), [ADR-0023](0023-rfc8212-real-policy-le32.md) (the policy the config carries), [ADR-0026](0026-per-cluster-derivation-from-index.md) (where the per-cluster values come from), [ADR-0017](0017-static-addressing-no-dhcp.md) (why node addresses are stable enough to list explicitly). Runbook: [`../pfsense-frr-bgp-setup.md`](../pfsense-frr-bgp-setup.md). Code: `ansible/playbooks/render-frr-config.yml`, `ansible/playbooks/templates/frr.conf.j2`.

## Context

pfSense CE 2.8.1's FRR package can be configured through GUI forms or through a
*Raw Config* field. The two are mutually exclusive: per Netgate, *"If you are
using Raw-Config to add commands, the GUI will not be able to control the
configuration. You need to delete Raw-Config and add the configuration via GUI
only."* There is no warning banner on the pages that quietly stop working. It
is an all-or-nothing choice.

The same pfSense box routes everything else in the house — this is live
production gear, not a lab router.

## Decision

### Raw config, generated — not the GUI

| | GUI | Raw config |
|---|---|---|
| `maximum-paths` (ECMP) | ✗ | ✓ |
| Config generated from the node map | ✗ | ✓ |
| Config reviewable in Git | ✗ | ✓ |
| GUI routing config still applied | ✓ | **✗** |

**`maximum-paths` alone justifies this.** It is
[Feature #16278](https://redmine.pfsense.org/issues/16278), open and
unassigned, and it is the only thing providing node-level load distribution —
without it one node wins best-path and takes 100% of ingress. `vtysh` is not
an alternative: per that report, *"Manual CLI changes via vtysh work until the
next GUI 'Apply,' which overwrites them."*

The total cost of raw config is acceptable because **we don't hand-maintain
the result**. `ansible/playbooks/render-frr-config.yml` renders `frr.conf` (the
paste target) and `bgp-nodes.txt` (firewall-alias members) from
`inventory/nodes.yml` + BWS. A generated, asserted, version-controlled config
is strictly more reviewable and reproducible than GUI forms, which are
unversioned by definition. Same Jinja2-from-node-map pattern the repo already
uses for Ignition. Delivery is a manual paste of the *whole* file, because
pfSense CE ships no API.

### Explicit `neighbor` statements — not `bgp listen range` (revised 2026-08-16)

Dynamic neighbors were originally the *second* headline reason for raw config:
any node in the DMZ subnet would be admitted automatically, so adding a node
would need zero pfSense changes. That was reversed. Why, in order of weight:

1. **All clusters share the DMZ subnet** (`node_number` uniqueness is global
   precisely because of this). A listen range maps one address prefix to one
   peer group, and a peer group carries exactly one `remote-as` *and* one set
   of prefix lists. So every cluster on that subnet would be forced onto **one
   ASN and one permitted LB range**, with each cluster free to advertise the
   other's. Splitting them would mean carving the /24 into per-cluster bands
   and constraining how `node_number`s are allocated forever after.
2. **The neighbor list and the firewall alias become the same list**, both
   rendered from `inventory/nodes.yml`, so they cannot drift.
3. Explicit neighbors are the tightest admission control available.

`remote-as external` / `auto` would technically admit multiple ASNs through
one listen range, but the peer group still carries one prefix list — so you'd
get the multiple ASNs *without* the isolation that was the reason to want
them, and lose the AS check that makes a misconfigured cluster fail loudly.

The shape: one peer group per cluster (carrying `remote-as` and the
`<CLUSTER>-IN`/`-OUT` prefix lists), one `neighbor <ip> peer-group <cluster>` +
`description <hostname>` per node, `timers bgp 3 9`, `maximum-paths 8`, no
`bgp bestpath as-path multipath-relax` (every node in a cluster shares one
ASN, so AS_PATHs are byte-identical — [ADR-0026](0026-per-cluster-derivation-from-index.md)).

### The LB supernet goes in the "internal networks" alias (2026-08-16)

Add the **`/16` supernet** (not the per-cluster `/24`) to whatever alias means
"internal networks". Isolated VLANs are typically written as `pass … to !
<HomeNets>` followed by a catch-all `block drop`; leaving the LB range *out* of
that alias grants those segments **unrestricted access to every LoadBalancer
service** — quietly, via a rule written years earlier for internet access.
Putting it in makes LB reachability **fail closed**. The supernet means every
future cluster's `<base>.<index>.0/24` inherits the policy automatically. Safe
to apply: an alias used only in `to ! <alias>` tests can never grant
reachability by growing, only remove it. Return traffic is unaffected — pf
creates a state pair for routed flows.

## Alternatives rejected

- **GUI-managed FRR** — cannot express `maximum-paths`; unversioned; not
  generatable.
- **`vtysh` for the missing knobs on top of GUI config** — overwritten on the
  next GUI Apply ([Bug #12928](https://redmine.pfsense.org/issues/12928)).
- **`bgp listen range` (dynamic neighbors)** — the original plan; rejected for
  the multi-cluster reason above.
- **Hand-written raw config** — rejected; the render asserts collisions
  (ASN uniqueness, ASN ≠ pfSense's, LB range vs DMZ/Ceph/pod/service CIDRs)
  that a human paste would not.
- **Per-cluster `/24` in the internal-networks alias** — the supernet covers
  future clusters without a checklist item.

## Consequences

- **The GUI still starts the daemons.** Raw config supplies the
  *configuration*, but the BGP tab's Enable is documented as the *"Master
  enable switch for BGP routing"* — leave it off and `bgpd` never starts, so
  the raw config is never read and the failure is silent. Enable-in-GUI,
  configure-in-raw. Fill in Local AS / Router ID in the GUI anyway so its state
  is coherent if anyone ever clears the raw config to fall back.
- **Adding a k3s node costs one pfSense paste** — re-render, paste the whole
  file. Not per *rebuild*: node IPs derive deterministically from
  `node_number`, so a destroy-and-recreate needs no pfSense change. With three
  workers planned and then stability, that's a small bounded number.
- **pfSense is a render target, not a source of truth.** A one-line edit in
  the raw config works — until the next render regenerates the whole file and
  reverts it on paste, silently. The worst case is a rotated credential:
  change the FRR master password on the box but not in BWS and the next paste
  quietly restores the old one. Whatever you change on pfSense, change in BWS
  too. Before any paste, diff the new render against what's running.
- **Raw config replaces the running `frr.conf` wholesale** — read what's
  already there first (`network` statements, `no bgp network import-check`,
  stale prefix lists, a different `router-id`). A config with no `neighbor`
  statements has no sessions, so replacing it disrupts nothing — verify with
  `show bgp summary` before assuming.
- **`show running-config` will not be byte-identical to the paste, and that's
  normal** — FRR rebuilds from internal state, strips every `!` comment, may
  reorder, and drops the redundant `neighbor <cluster> activate` (`frr
  defaults traditional` implies `bgp default ipv4-unicast`). Compare meaning,
  not text; check `le 32` survived.
- **🔁 Re-verify after every FRR package or pfSense upgrade.** Raw config is
  more fragile across upgrades precisely because the package isn't
  regenerating it; [Bug #7859](https://redmine.pfsense.org/issues/7859) was a
  config-tag rename that caused raw config to be *silently ignored*.
- **A parked config lists every node in `Active`/`Connect`, retrying** —
  expected and harmless before the cluster exists, and useful for confirming
  the peer list. (Under the old listen-range plan the opposite held — an empty
  `show bgp summary` was success. Old notes saying that no longer apply.)
- `ansible/.frr/` is git-ignored — `frr.conf` embeds the FRR master password,
  written `0600` in a `0700` dir. `-e frr_redact_password=true` renders a
  shareable copy. Redact the `password` line before pasting `show
  running-config` anywhere.
- The play derives each neighbor address as `{{ dmz_network.subnet_base
  }}.{{ node_number }}` — the **same** derivation as `preflight.yml`'s
  `eth0_ip`, duplicated in the one other place that needs it. Change both or
  neither.
- The render cannot see the pfSense interface list, so "the LB range is
  attached to no interface" ([ADR-0026](0026-per-cluster-derivation-from-index.md))
  stays a manual check.
- Blast radius is contained: nothing is redistributed (`-OUT` denies all, no
  `network` statements), no session means no routes, and a syntax error means
  FRR doesn't start — static/kernel routing untouched. Rollback is unchecking
  Enable FRR. Take a config backup first.

## Evidence

Rendering verified for one and two clusters, and the collision asserts verified
*firing* rather than merely passing. The paste landed and the session was
verified live on 2026-08-16 (`Established`, `PfxRcd 1`, `PfxSnt 0`, both
advertisement modes accepted). See [`../worklog.md`](../worklog.md) and the
runbook §5–§7.
