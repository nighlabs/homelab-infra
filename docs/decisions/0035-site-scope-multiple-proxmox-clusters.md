# ADR-0035: A third scope — **site** (one Proxmox cluster + its Ceph) — between fleet-wide and per-k3s-cluster

- **Date:** 2026-09-12 (raised) · 2026-09-13 (three rules settled)
- **Status:** **Open** — the site *implementation* is deferred until a second Proxmox cluster is real. ⚠ But three rules are **decided** and are not open: (1) suffix by owning scope, vaults are for access rather than naming; (2) `node_number` and hostnames stay globally unique **as a requirement**, not as a consequence; (3) `base_domain` and `cloudflare_api_token` are scoped together, on the DNS zone.
- **Supersedes / related:** [ADR-0026](0026-per-cluster-derivation-from-index.md) (per-cluster derivation from `index` — the precedent this extends, and the record whose "one number to change" property must survive), [ADR-0034](0034-secrets-store-1password.md) (the store this is expressed in; its per-cluster apps vaults are a *different* axis — see below), [ADR-0033](0033-secrets-fact-broker.md) (the flat `secrets` namespace this relies on), [ADR-0006](0006-ceph-csi-external-proxmox-ceph.md) (ceph-csi against *the* existing Proxmox Ceph — singular, and that is the assumption), [ADR-0017](0017-static-addressing-no-dhcp.md) (node addressing derived from `node_number`), [ADR-0022](0022-pfsense-frr-raw-config-explicit-neighbors.md) (one pfSense/FRR). Code: `ansible/inventory/nodes.yml`, `ansible/inventory/group_vars/all/vars.yml`, `ansible/playbooks/tasks/load-node-map.yml`, `ansible/playbooks/render-ceph-setup.yml`, `ansible/playbooks/render-frr-config.yml`.

## Context

The repo models **two** scopes today: fleet-wide, and per-k3s-cluster
(ADR-0026's `clusters:` keys, with cluster-suffixed secrets
`k3s_token_<cluster>` / `k3s_tls_sans_<cluster>`).

It has been careful about *that* boundary — `calico_version` is marked
deliberately fleet-wide, and `base_domain` carries an explicit "⚠ FLEET-WIDE
TODAY, and that flat shape BREAKS AT CLUSTER #2" note with its fix sketched.

**But a whole class of values is neither, and is currently assumed singular
without being flagged at all:** the Proxmox cluster, its Ceph, and the L2 they
sit on. `proxmox_api_host`, `proxmox_node`, `proxmox_vm_storage`, `ceph_mons`,
`ceph_fsid`, `ceph_fs_name`, the two cephx keys, `dmz_network`,
`ceph_public_network`, `dns_servers`, `frr_master_password` are all flat scalars
in `vars.yml`. That is correct for one Proxmox cluster and wrong for two.

The prompt: **multiple Proxmox clusters, each with its own Ceph**, while SSH
keys stay fleet-wide. That is a third scope, and it does not decompose into the
two that exist — several k3s clusters may share one Proxmox cluster, so it is
neither fleet nor per-k3s-cluster.

⚠ **Not the same axis as ADR-0034's per-cluster apps vaults.** That split is
about *who reads a secret* (each cluster's ESO). This is about *what a value
describes*. They are orthogonal and both can be true at once.

## The three scopes, and the rule that decides which one applies

**Suffix by the scope that OWNS the value, never by the scope that consumes
it.** A k3s join token is per-cluster because a cluster owns it. A Proxmox API
host is per-**site** because the site owns it — suffixing it per-cluster would
duplicate one value across every cluster on that site, which is exactly the
"two sources that diverge silently" failure ADR-0027 was written to repair.

| Scope | Owns | Values |
|---|---|---|
| **Fleet** | true everywhere, one copy | `ssh_authorized_keys` (confirmed: the same keys everywhere), `mac_oui`, `bgp_asn_base`, `bgp_peer_asn`, `k3s_version_default`, `calico_version` |
| **DNS zone** ⚠ its own axis | the Cloudflare zone | `base_domain`, `cloudflare_api_token` — see below |
| **Site** = one Proxmox cluster + its Ceph + its L2 | the hypervisor, the storage, the wires, the router | `proxmox_*` (api host/user/token/node, storages, snippet dir, ssh addr/user), `ceph_*` (mons, fsid, fs_name, both cephx keys, subnet/vlan/bridge), `dmz_*` (subnet/vlan/bridge/gateway), `dns_servers`, `frr_master_password`, `lb_range_base` |
| **k3s cluster** | the cluster itself | `k3s_token_<cluster>`, `k3s_tls_sans_<cluster>`, `index` (→ ASN, LB range), per-cluster `k3s_version`, and `base_domain` if it forks |

**A k3s cluster belongs to exactly one site; a site hosts many k3s clusters.**
`inventory/nodes.yml` would gain `site: <name>` per cluster (defaulting to the
single site, so today's file is unchanged), and that key is the join.

### ⚠ `cloudflare_api_token` is not fleet-scoped — it is ZONE-scoped

A correction to the table's first draft, and to `vars.yml`'s note, which
undersold it as *"can fork per cluster the same way if per-cluster revocation is
wanted"*. It is not a preference. **The token is scoped in Cloudflare to
specific zone resources** (ADR-0013: Zone > DNS > Edit *and* Zone > Zone > Read,
on that one zone), so a token for zone A simply **cannot** solve a DNS-01
challenge for zone B. If `base_domain` ever forks, the token must fork with it —
a correctness constraint, not a revocation nicety.

The scope it belongs to is therefore neither site nor cluster but **the DNS
zone**, and which of those that maps onto depends on a choice not yet made:

- **Subdomains of one zone** (`<cluster>.example.net`) — one zone, so **one
  token** scoped to the parent zone serves every cluster and every site. The
  token stays effectively fleet-wide.
- **Genuinely separate zones per cluster or per site** — one token each, named
  on whichever axis the zone follows.

⚠ So `base_domain` and `cloudflare_api_token` must be scoped **together and on
the same axis**; splitting one without the other yields a cluster that can name
a hostname it cannot get a certificate for.

## What this would cost — and the part that is already free

### ⚠ The store needs no new machinery at all

This is the good news, and it is a direct consequence of ADR-0033 + ADR-0034:

- **Field labels stay one FLAT global namespace**, suffixed by owning scope:
  `proxmox_api_host_<site>`, `ceph_mons_<site>`, alongside
  `k3s_token_<cluster>`. Exactly the existing idiom.
- **Items are organisational only** — a `control-node` item for fleet values and
  a `site-<name>` item per site. ⚠ An item cannot be the namespace: labels must
  be unique across everything merged into one `secrets` dict, so two `site-*`
  items could not both carry a bare `proxmox_api_host`. **The suffix
  disambiguates, not the item.**
- Cost: one extra `op item get` per site — `1 + N` calls per play, nowhere near
  the 1,000/day cap.
- `tasks/load-secrets*.yml`, the `secrets` fact, `secret_names` and the broker
  are all **untouched**. Only `vars.yml`'s derivations change.

### The alternative: **vaults** as the namespace

Items cannot be a namespace, but **vaults can** — a `site-<name>` vault per
site, each holding bare `proxmox_api_host`, `ceph_mons`, … with no suffix at
all. 1Password imposes no cross-vault uniqueness; the only thing that collides
is *this repo's* decision to merge everything into one flat `secrets` fact. So
the loader would key by vault (`site_secrets[<site>].proxmox_api_host`) instead
of merging, and `vars.yml` would read

    proxmox_api_host: "{{ site_secrets[cluster_site].proxmox_api_host }}"

which is arguably cleaner than a suffix lookup.

**What it genuinely buys:** vaults are 1Password's *access* boundary, so a
per-site vault makes "this credential may manage site A only" expressible — a
scoped operator, or a CI runner for one site. A suffix cannot express that; a
credential that can read one suffixed field can read them all.

**What it costs:**

- The flat `secrets` contract from ADR-0033 gains a second, nested shape. Two
  contracts instead of one, and `secret_names` needs the same treatment.
- The loader changes — currently a deliberate non-goal, since ADR-0033's whole
  point is that the store is reachable from two files and stays there.
- More immutable service-account grants to get right up front, per ADR-0034.

**Recommendation — keep them separate concerns:**

> **Vaults answer "who may read this." Suffixes answer "what does this
> describe."**

That is already what the repo does everywhere else, and it is why ADR-0034 puts
*apps* in per-cluster vaults (a genuine access boundary: each cluster's ESO)
while k3s join tokens live as **suffixed fields** in the shared infra vault (no
access boundary — one control node reads them all). Sites are the second case:
the control node provisions *every* site, so there is no boundary to draw, and a
per-site vault would be a namespace wearing an access-control costume.

⚠ **Revisit if a scoped-per-site operator ever becomes real** — that would turn
sites into a genuine access boundary, and then vault-as-namespace becomes the
right answer for the same reason it is right for ESO.

### What actually breaks, concretely

Not "it would need work" — these are the specific things that are wrong at
site #2:

1. **`vars.yml`: every site-scoped scalar becomes a per-site lookup.** The
   mechanism already exists — the single-expression Jinja loop used by
   `k3s_tokens` and `cluster_bgp` — but it is ~20 variables, and `dmz_network` /
   `ceph_public_network` / `ceph_csi` are dicts, so they become maps keyed by
   site.
2. ✅ **`node_number` and hostname uniqueness: DECIDED — they stay GLOBAL.**
   ⚠ But the *reason* must change, and that is the whole point. Today
   `tasks/load-node-map.yml` justifies it as "all clusters share the DMZ/Ceph
   subnets and the Proxmox vmid space" — a *derivation* from the single-site
   setup, which becomes false at site #2 and would make the guard look wrong
   when it is right. It is now a **requirement**: a node may move to another
   Proxmox node, another site, or a completely separate network, and a globally
   unique number means that move is never an identity change. Policy, not
   consequence — so the assert stays exactly as it is, and only its comment
   changes. (Uniqueness stays global; `vmid`'s and the subnet's *collision
   domains* really are per-site, which is why the reason had to be restated
   rather than reused.)
3. **`vmid = 1000 + node_number`** stays collision-free as a consequence of the
   decision above, not as an independent guarantee: its real collision domain is
   one Proxmox cluster, and global `node_number` uniqueness is simply stricter.
4. **`render-ceph-setup.yml` renders one set of scripts for one Ceph.** It would
   render per site, into `.ceph/<site>/`. ⚠ Its "the cluster-side secrets do not
   exist yet on a first run" softness has to stay per-site, or adding site #2
   makes site #1's render start failing.
5. **`render-frr-config.yml` assumes one pfSense/FRR.** Each site has its own
   router, so ASN and LB-range collision asserts are per **routing domain** =
   per site. Two sites may legitimately reuse an LB range if they never share a
   router — but that must be a decision, not an accident.
6. **`proxmox_node` is a single node name**, and `module_defaults` in every play
   binds one set of API credentials per play — so a play would need to group its
   loop by site, or run per site.
7. **`bootstrap-cluster.yml` seeds `cluster-topology`** from these values. No
   `gitops/` change: the manifests already carry `${placeholders}`, and the
   Secret is seeded per cluster — it would simply be seeded from that cluster's
   *site*. ⚠ Worth stating, because "multi-site" sounds like it should ripple
   into Flux and it does not.

## Options

- **Do nothing until a second Proxmox cluster is real.** The repo's own
  precedent for exactly this (`calico_version`, `base_domain`): flag it, sketch
  the fix, defer. Costs a rename of ~15 1Password fields plus the `vars.yml`
  rework when it lands — and the `vars.yml` rework is required regardless, so
  the rename is a small part of a change that has to happen anyway.
- **Suffix everything `_<site>` now, with one site.** Forward-compatible, but
  adds `_homelab`-shaped noise to every name for a second site that may never
  exist, and buys nothing until it does. It does not avoid the `vars.yml`
  rework — flat `secrets.proxmox_api_host_dc1` is still a flat scalar until the
  lookup is written.
- **Introduce `site:` in `nodes.yml` now, defaulted, with values still flat.**
  A middle path: the *join* exists and is documented, the values follow later.
  ⚠ Cosmetic on its own — a `site:` key nothing reads is a comment with
  punctuation.

## Recommendation, pending a decision

**Defer the implementation; settle the rules now.** Three are settled here and
need no second pass:

1. **Suffix by owning scope; vaults are for access, not naming** (the table, and
   the vault-as-namespace section).
2. **`node_number` and hostnames stay globally unique — as a requirement**, so a
   node can move between Proxmox nodes, sites or networks without changing
   identity. The guard is unchanged; its justification is rewritten from
   derivation to policy.
3. **`base_domain` and `cloudflare_api_token` are scoped together, on the DNS
   zone.** Subdomains of one zone keep both effectively fleet-wide.

What is deferred is the `vars.yml` rework and the per-site rendering, on the
same grounds this repo already applies to `calico_version` and `base_domain`:
one Proxmox cluster exists, and the rework is required at site #2 regardless, so
the field rename is a small part of a change that has to happen anyway.

⚠ The expensive failure mode was never the refactor — it is *discovering at
site #2 that a guard's stated reason was an assumption nobody labelled*. Item 2
was exactly that, and it is now fixed in place rather than left to be
rediscovered.

## Evidence

Nothing implemented. Established by inspection on 2026-09-12: `nodes.yml` and
`tasks/load-node-map.yml` both state the shared-subnet/shared-vmid assumption
explicitly; `vars.yml` carries ~20 flat site-scoped scalars and three
site-scoped dicts; `grep` for `FLEET-WIDE` finds the boundary flagged for
`calico_version` and `base_domain` only. One Proxmox cluster and one Ceph exist
today, so none of this is currently wrong — only unlabelled.
