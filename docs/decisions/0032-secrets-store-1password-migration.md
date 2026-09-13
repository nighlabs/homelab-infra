# ADR-0032: Whether to replace Bitwarden Secrets Manager with 1Password

- **Date:** 2026-09-12 (raised)
- **Status:** **Answered by [ADR-0034](0034-secrets-store-1password.md)** (2026-09-12) — decided in favour of moving, before the ESO milestone as this record urged. Retained as the investigation the decision rests on. ⚠ Two of its recommendations were *not* followed, deliberately: the Python SDK module (the `op` CLI is used instead) and mandatory service-account auth (the control node uses the desktop app and keeps no secret zero). ADR-0034 records why.
- **Supersedes / related:** [ADR-0027](0027-control-node-secrets-bws-runtime.md) (control-node secrets from BWS at run time — this would supersede it), [ADR-0009](0009-secrets-aescbc-and-eso-bitwarden.md) (its *layer 2*, ESO + Bitwarden, is unbuilt; layer 1, aescbc at rest, is unaffected), [ADR-0021](0021-topology-blinding-postbuild-substitution.md) (its `cluster-topology` reasoning is corrected below), [ADR-0033](0033-secrets-fact-broker.md) (the broker seal, worth doing either way), [ADR-0015](0015-backups-nas-s3-and-break-glass.md) (the break-glass export mechanism would change). Code: `ansible/library/bws_secrets.py`, `ansible/playbooks/tasks/load-bws-secrets.yml`, `ansible/BWS-SECRETS.md`.

## Context

Every secret reaches this repo from Bitwarden Secrets Manager: the control node
reads it at run time via a custom bulk-fetch module (ADR-0027, live since
2026-08-17), and the unbuilt ESO milestone is designed around the Bitwarden
provider and its in-cluster **Bitwarden SDK Server** (ADR-0009).

The question raised: what would it take to move to 1Password instead, using the
**ESO 1Password SDK provider**? (1Password Connect carries a deprecation notice
pointing at the SDK provider, so Connect is not a candidate.)

Nothing has been decided. This record exists because the investigation turned
up two things that outlive the question — a documentation claim that is
**already wrong today**, and a dependency edge that is **weaker than the repo
asserts** — and because the answer materially changes if it is asked after the
ESO milestone rather than before.

## What the investigation established

### The cluster half is greenfield, not a migration

There are no ESO manifests, no SDK Server, no `SecretStore` anywhere in
`gitops/` — only reserved slots in `gitops/infrastructure/kustomization.yaml`,
`gitops/infrastructure-config/kustomization.yaml` and `gitops/CLAUDE.md`.

So "swap the cluster side" is not a migration; it is choosing a different
provider at a milestone that has not started. **On that side 1Password is
strictly less work than Bitwarden would have been** — no SDK Server Deployment,
Service, `Certificate`, or image digest to pin and maintain. That asymmetry is
the strongest argument for deciding *before* the milestone: porting ESO twice
is the expensive path.

### ⚠ The "ESO cannot be pulled earlier" chain is partly overstated *today*

ADR-0009, ADR-0021, the root `CLAUDE.md` and `architecture.md` §3.6 all state
the chain as: SDK Server → cert-manager cert → Gateway → LoadBalancer IP → BGP.

**The Gateway and LoadBalancer links do not belong in that chain.** This repo
issues certificates by ACME **DNS-01** through the Cloudflare API (ADR-0013).
DNS-01 solves without any inbound path, so it needs no Gateway and no LB IP;
those are what *serving* the wildcard cert needs, not what *issuing* it needs.
An internal-CA or self-signed issuer would need even less.

This is true regardless of 1Password, and it is corrected in the reference docs
as part of raising this record. It is left standing in ADR-0009 and ADR-0021,
which are not edited to change the past — this ADR is where the correction
lives.

With the 1Password SDK provider the question becomes moot rather than resolved:
there is **no in-cluster server and therefore no certificate at all**. What
still constrains ESO's position is only: CRDs Established, Calico and pod
networking, egress to the vendor API (pod→node NAT — *not* BGP, which
advertises LoadBalancer ranges inbound), and its own Ansible-seeded token
`Secret`. ESO's admission webhook certs come from its own bundled
`cert-controller`, never cert-manager.

**Net: ESO's earliest possible position moves from "after cert-manager, NGF and
BGP" to "immediately after Calico" — exactly where the reserved slots already
put it. One edge dissolves; no re-tiering is required.**

### Three consequences for records that already exist

- **`cluster-topology` stays Ansible-seeded permanently, but ADR-0021's stated
  reason would become false.** The conclusion survives on stronger ground:
  Flux evaluates `postBuild.substituteFrom` at build time for the
  `infrastructure` tier, and kustomize-controller applies a tier in **one pass
  with no intra-tier ordering** — so a `Secret` produced by a controller
  *inside* that tier can never be a substitution source *for* that tier. And
  Calico is Ansible-primed from these same values before Flux exists at all
  (ADR-0016), so the control node holds them regardless. *Cluster-bound still
  does not mean ESO-managed.*
- **The ESO-adoption open question collapses to a single case.** It currently
  turns on an asymmetry: cert-manager's Cloudflare token is "genuinely upstream
  of ESO", so retiring its seed is off the table, while ceph-csi's cephx keys
  are not upstream, so retiring theirs is on it. Remove the SDK Server and
  cert-manager stops being upstream too — both become the same question,
  governed only by rebuild-path length, which is now short.
- **The `infrastructure-config` split question is demoted, not answered.** Its
  one *hard* forcing edge is "ESO's SecretStore needs cert-manager's
  `Certificate`", and that edge disappears. The soft costs remain (one Ready
  condition spanning three failure domains, one timeout tuned for ACME, cert
  renewal gating `apps`), so it becomes an observability argument with no
  correctness argument behind it. ⚠ The only way to reintroduce a hard edge is
  to choose an ESO-sourced Cloudflare token — so these two open questions are
  now **coupled and must be decided together**.

### ⚠ The rate limit is where 1Password is genuinely worse

1Password enforces a daily cap **per account, shared across every service
account** — so the control node and ESO draw from one bucket:

| Plan | Requests / 24 h (account) | Reads / hour (token) |
|---|---|---|
| Individual & Families | **1,000** | 1,000 |
| Teams | 5,000 | 1,000 |
| Business | 50,000 | 10,000 |

The Ansible side fits comfortably on any plan: with the grouped-item layout
below, ~5 calls per play, ~20 for a full `site.yml`. Fifty runs a day still
fits inside 1,000.

**ESO is where it bites.** Steady state is roughly
`N_externalsecrets × (24h / refreshInterval)` less cache hits — at the default
`1h` with 20 `ExternalSecret`s that is ~480/day, about half of an Individual
account's entire allowance, before the Postgres → Redis → LiteLLM stack is even
built.

⚠ **The real hazard is error amplification, not steady state.**
[external-secrets#4925](https://github.com/external-secrets/external-secrets/issues/4925)
records an invalid token consuming quota at retry cadence and exhausting a
**daily** limit in ~20 minutes. On Business that is noise. On Individual it is
a **24-hour lockout of ESO *and* the control node** — including the playbook
you would use to fix it. ADR-0027's "provisioning must work offline is a
non-scenario" argument covers a network outage; it does **not** cover
self-inflicted quota exhaustion, which has no offline path.

Bitwarden's limits are *undocumented* (ADR-0027 cites throttling reported after
six calls in succession). Published limits you can design against are better
than unknown ones you discover by tripping them — but 1,000/day is a real
ceiling BWS never imposed in practice. **On this axis specifically, the move is
a regression below the Teams tier.**

## Options

- **Stay on BWS.** Costs nothing, and the control-node half is live and
  verified. Accepts that the ESO milestone builds and maintains an in-cluster
  SDK Server with a cert-manager certificate.
- **Full replacement, control node first.** The shape considered here; sketched
  below. Staged in *time*, never by vendor.
- **1Password for ESO only, BWS retained on the control node.** ⚠ **Reject.**
  It takes the SDK-Server win without touching working Ansible, but costs two
  stores, two secret zeros, two manifests to keep reconciled, and no single
  store to split by consumer — which is what makes the "cluster compromise must
  not reach the Proxmox token" argument clean. ADR-0027's Context is *literally*
  about the cost of two contradicting sources of truth.

## What full replacement would involve

**Control-node module.** A new `ansible/library/op_secrets.py` keeping the
existing output contract (`secrets` / `count` / `names`) exactly, so every
consumer changes only by ADR-0033's rename. The 1Password Python SDK is async,
so `asyncio.run()` wraps the fetch, with narrow exception translation — a bare
async traceback in `fail_json` is unreadable and can leak the token in a repr.

**Item layout — and a reversal of ADR-0027 on its own terms.** ADR-0027
rejected grouping values on one explicit premise: *"a BWS secret has no
fields"* — Name + Value + Notes, where Value is a single opaque string, so
grouping meant hand-authoring JSON into a textarea with no validation.
**1Password removes that premise**: items have first-class fields with a label
and a type (`text` / `concealed`), each independently editable in a real form.
The rejection does not survive contact with the new store, and a future ADR
must record that as a reasoned reversal rather than silently.

Two items in the control-node vault, with purpose-grouping as *sections*: one
`control-node` item carrying every value in Proxmox / Networks / Access / Edge
/ Storage / Clusters sections, and a separate `eso-service-account` item so
that token stays visibly and separately rotatable. Seven items would cost ~10
calls per play; one item couples the ESO token's history to everything else;
two costs ~5. Field labels must stay globally unique — they already are, and
the module must hard-fail on duplicates, matching both the existing guard and
ESO's own "found multiple labels with the same key" constraint.

Multi-line values (`dns_servers`, `ssh_authorized_keys`, `ceph_mons`,
`k3s_tls_sans_*`) keep the one-per-line convention: 1Password has no list type
either, so JSON would reintroduce hand-authoring for no gain.

**Addressing.** SDK item methods take IDs; only `secrets.resolve` takes names,
and that is one call per field — `bitwarden.secrets.lookup` wearing a new hat,
rejected for the same reason and worse under a hard cap. Use
`vaults.list()` → `items.list()` → `items.get()`, with pinned-UUID overrides
available. The two list calls buy real value: BWS has no list operation, which
is why the current module needs a hand-written 404 hint, whereas here the
module can say *"no vault by that title; this account can see: …"*.

**Secret zero.** The same macOS Keychain mechanism, item renamed to the
vendor's own `OP_SERVICE_ACCOUNT_TOKEN`, same `-e` > env > Keychain precedence.
Every hard-won gotcha in ADR-0027 transfers unchanged. There is **no
organization-id equivalent** — the token encodes the account — so the vault
*title* becomes a committed default, deleting one Keychain item, two tasks,
and the entire org-id-vs-project-id 404 trap with its diagnostics.

⚠ Do **not** make `op` CLI biometrics the default: it authenticates as *you*,
with your full vault access, discarding the least-privilege property the
consumer split exists to protect, and it is interactive-only. Touch ID is
available anyway with **no repo change**, by keeping the scoped token and
exporting it from a personal item — the existing precedence chain already
honours an environment variable.

**Vault layout.** ADR-0027's split *by consumer* is preserved verbatim, and
gets structurally stronger: a SecretStore names exactly one vault and cannot
reach a second, so "cluster compromise escalates to hypervisor compromise" is
closed by the API shape rather than by discipline.
`eso_op_service_account_token` still lives in the control-node vault — *the
thing that grants access cannot live behind the access it grants.*

⚠ **Service-account permissions and vault access are immutable after
creation**, and neither vault may be a built-in Personal or default Shared
vault. Immutability collides with the adoption open question, whose current
lean has the control-node account reading *both* projects: that grant cannot be
added later. **Decide adoption before creating the accounts.**

**Budget headroom.** 100 service accounts and unlimited vaults, against BWS's
3 projects / 3 machine accounts — the squeeze that forced deleting the
migration write account disappears.

## Consequences if it goes ahead

- ADR-0027 is superseded; ADR-0009's *layer 2* is superseded while **layer 1
  (k3s aescbc at rest) is untouched and live**. ⚠ That needs a status form the
  index does not yet have — recommend `Partially superseded by ADR-NNNN (layer
  2 only); layer 1 unchanged and live` over restating layer 1 verbatim
  elsewhere.
- `ansible/BWS-SECRETS.md` becomes a vendor-neutral `ansible/SECRETS.md`,
  restructured into the item/field layout, with the missing
  `eso_*_access_token` row added. Its "a BWS secret has no fields" paragraph is
  **inverted, not deleted** — it becomes the reason grouping is correct.
- `bitwarden-sdk` gives way to `onepassword-sdk`, pinned by **minor**: it is
  0.x with documented breaking changes between minors (worse than the current
  1.x) and carries a native dependency (libssl 3, glibc 2.32+) that constrains
  a Linux control node or CI runner.
- ADR-0015's break-glass "periodic encrypted export" survives as an argument;
  the mechanism changes.
- Per-access audit becomes a paid-tier reporting feature. ⚠ Worth re-checking
  ADR-0009's symmetrical "logs every access" claim about Bitwarden, since its
  event logs are also an Enterprise feature — the old claim may not have been
  accurate on the free tier either.
- Rollback stays cheap through cutover: both modules present, gated on a
  variable, with a byte-diff of `render-frr-config.yml`'s output across
  backends as the parallel-run proof. The parallel window is where all the
  safety lives.

**Rough cost: 3–4 focused days, more than half of it prose.** The module is
~1 day; 1Password data entry ~1 hour; the rename and task file 1–2 hours;
parallel-run verification 2–4 hours; the documentation sweep 1–2 days. The
prose dominates because this repo's docs are unusually cross-referential, and a
sloppy sweep leaves exactly the contradictions ADR-0027 was written to repair.
⚠ `docs/worklog.md` is append-only — a `grep -rl | xargs` sweep would corrupt
it.

## The decision, stated plainly

The dependency-cycle removal is the real prize and it is worth having *before*
the ESO milestone. But the rate-limit finding means the cluster half
realistically needs a paid tier above Individual/Families — so this is a
question about a recurring subscription, not only about engineering effort.

**Doing nothing has a deadline**: once ESO ships on Bitwarden, the cheap
version of this decision is gone.

[ADR-0033](0033-secrets-fact-broker.md) is deliberately split out because it is
correct either way, and it turns whatever gets decided here into a two-file
change.

## Evidence

Nothing implemented; nothing to verify. Established by inspection on
2026-09-12: `feat/eso` at `5da978d` was zero commits ahead of `main` with a
clean tree and no ESO manifests; the four `bws.*` leak sites were confirmed by
grep (see ADR-0033); the DNS-01 correction follows from ADR-0013 and the live
`ClusterIssuer` configuration. Vendor limits, the SDK provider's schema, and
the absence of an in-cluster server component were read from current upstream
documentation on the same date and should be re-checked before acting.
