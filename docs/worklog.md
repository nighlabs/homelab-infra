# Worklog

Chronological record of milestones, verifications, and the failures found along
the way — newest first. Reference material lives in
[`architecture.md`](architecture.md) and the per-directory `CLAUDE.md`/`README.md`;
the reasoning behind each choice lives in [`decisions/`](decisions/README.md).
This file is append-only history: entries are never rewritten when later
superseded — a later entry says so.

Blinding applies here as in every committed doc: node addresses appear as
`x.x.x.N`, topology as `${placeholder}`. Hostnames (`snoop-a2o`, `phoenix-1`)
and private-range ASNs are fine.

---

## 2026-09-12 — Moved the secret store to 1Password, read with the `op` CLI; the control node now keeps no secret zero

**Related:** [ADR-0034](decisions/0034-secrets-store-1password.md) (new) ·
[ADR-0032](decisions/0032-secrets-store-1password-migration.md) (answered) ·
[ADR-0027](decisions/0027-control-node-secrets-bws-runtime.md) (superseded) ·
[ADR-0009](decisions/0009-secrets-aescbc-and-eso-bitwarden.md) (layer 2 only) ·
[ADR-0033](decisions/0033-secrets-fact-broker.md) · Code:
`ansible/playbooks/tasks/load-secrets*.yml`,
`ansible/playbooks/render-1password-import.yml`, `ansible/SECRETS.md`.

ADR-0032 raised this as an Open question and stopped there. Decided in favour,
and implemented the control-node half the same day — the broker seal above is
what made it a two-file change instead of a ~40-site sweep.

**Three reversals, all deliberate, all recorded rather than glossed:**

| Reversed | Because |
|---|---|
| ADR-0027: "shelling out is brittle **when the SDK is already a dependency**" | It isn't. 1Password's Python SDK would be a *new* 0.x dep with breaking minors and a native libssl/glibc requirement; `op` is a static Go binary and `--format json` is a contract, not a scraped table. |
| ADR-0027: grouping rejected because **"a BWS secret has no fields"** | 1Password items have typed, labelled fields. The premise is void, so grouping is now correct rather than a JSON hack — and it is what keeps the call count at one per item. |
| ADR-0032: **"do not make `op` biometrics the default"** | Overruled *for the control node only*. Secret zero doesn't relocate — it ceases to exist. ESO still uses a scoped service account, because a pod has no desktop app. |

**The auth change is the one worth remembering.** With the desktop-app
integration there is no token on disk, in a keychain, or in the repo. The trade
is real and is stated in the ADR: `op` then authenticates as the operator, with
read/write on every vault they own. It is acceptable because the operator at the
keyboard already had that access — the token was a boundary against a stolen
laptop, not against them, and a silently-readable Keychain item was a *worse*
answer to that threat than Touch ID plus a session timeout. It also deletes the
immutable-grant trap from this side: with no control-node service account, the
only fixed grant left is ESO's.

**Migration by rendering, not retyping.** `render-1password-import.yml` reads
the old store and renders a 1Password item template — same shape as
`render-ceph-setup.yml`, same reason. ⚠ A *template file*, not `op item create
name=value`, because 1Password's own CLI warns that assignment statements are
visible in argv and shell history. It renders rather than writes because
creating the item needs write access, which no run-time credential here has.

**Verification so far** (live BWS store, 29 secrets, against a `.frr`/`.ceph`
baseline taken before any change):

| Check | Result |
|---|---|
| Import renderer output | valid item JSON; 29 fields → 6 sections, **nothing** in the catch-all (asserted, not observed); labels unique; both STRING and CONCEALED present |
| Bitwarden backend through the new dispatcher | byte-identical renders, re-checked after every commit — the control for the whole migration |
| 1Password path reaches `op` | reports desktop-app mode and the correct "enable the integration" instruction with no account configured; `op` 2.39.0 clears the >= 2.18.0 assert |
| `--syntax-check`, all seven playbooks | pass |

⚠ **The parallel run has NOT happened** — no vault or item exists yet. Until
`render-frr-config.yml` and `render-ceph-setup.yml` are byte-identical under
*both* backends, the Bitwarden half stays. ADR-0034 lists what gets deleted, and
it is deleted together.

**Two findings that outlive the migration:**

- ⚠ **A latent ADR-0027 bug, exposed the first time it mattered.** The Keychain
  `pipe` lookup turns `security`'s exit-44 into a hard AnsibleError *while
  resolving the variable* — before the assert that explains how to create the
  item. So on any machine without the item, the carefully-written "here is the
  `security add-generic-password` command" message **could never render**; you
  got `lookup_plugin.pipe(...) returned 44`. Same shape as the
  `render-ceph-setup.yml` trap found hours earlier: a good error message behind
  a failure that fires first. Fixed on the 1Password path; the masking cost (a
  locked keychain now reads as "no token") is named in the fail_msg.
- ⚠ **`op` treats the PRESENCE of `OP_SERVICE_ACCOUNT_TOKEN` as mode selection.**
  Exporting it empty selects service-account mode and then fails to
  authenticate, instead of falling back to the desktop app — and the resulting
  error talks about a bad token on a machine that was never meant to have one.
  The task therefore builds the environment conditionally rather than passing
  `""`. Caught by reasoning about the fallback, not by hitting it.

**Also corrected while sweeping:** "ESO sits below cert-manager" is now **false**
— no in-cluster server, no certificate — so both copies of that rule were
replaced, each noting where it came from so it isn't reinstated from memory.
`cluster-topology` was split into its own bullet: it had been written as a rider
on that chain, but survives for unrelated reasons (one-pass tier application;
Calico primed before Flux), and leaving it attached to a now-false claim would
have made it look retired. The `infrastructure-config` split question lost its
only hard forcing edge as a result, and is now coupled to the ESO-adoption
question — they must be decided together.

⚠ **The sweep was scoped by explicit file list**, never `grep -rl | xargs`:
`docs/worklog.md` is append-only and `docs/decisions/*` are records. ADR-0032
warned about exactly this.

**Corrected on review: the apps side is now ONE VAULT PER CLUSTER.** The first
pass carried ADR-0027's single shared apps project straight across. That shape
was a **budget artefact, not a judgement** — BWS capped the free tier at 3
projects / 3 machine accounts, which is the same squeeze that forced deleting
its migration write account. 1Password allows 100 service accounts and unlimited
vaults, so the per-cluster split the blast-radius argument always wanted is
simply affordable: `<cluster>-apps`, one ESO service account each, and
`eso_op_service_account_token_<cluster>` in the infra vault, cluster-suffixed
exactly like `k3s_token_<cluster>` (ADR-0026). ⚠ The infra vault stays
**fleet-wide** on purpose — the control node provisions every cluster, so
splitting it would fragment one Proxmox credential across vaults the same actor
reads anyway. No code change: the loader reads one vault and that is still
correct; ESO's SecretStore names its own.

⚠ **A guard added with it.** `render-1password-import.yml` renders ONE item
holding every secret, titled `op_items[0]` — right for the migration (BWS had
29 flat secrets, no items), wrong the moment `op_items` grows for the ESO item,
when the extra titles would be silently ignored and their fields swept into the
first item. It now refuses, and says the real answer: by then this one-shot play
should already be deleted. Verified both ways.

**Raised while reviewing the vault layout: a THIRD scope the repo doesn't have**
— [ADR-0035](decisions/0035-site-scope-multiple-proxmox-clusters.md), Open.
Everything is fleet-wide or per-k3s-cluster today, but `proxmox_*`, `ceph_*` and
`dmz_*` are neither: they belong to a **site** (one Proxmox cluster + its Ceph +
its L2), and several k3s clusters can share one. Nothing is wrong today — there
is one of each — but the assumption was stated as a *fact* in two places, which
is the expensive kind. ⚠ Concretely: the global `node_number` uniqueness assert
is justified in `load-node-map.yml` by "all clusters share the DMZ/Ceph subnets
and the Proxmox vmid space", so at site #2 it goes from correct to
**wrong-by-being-too-strict** — it would reject a valid node map, and the
tempting fix (loosen the assert) is the wrong one. Both sites of that assumption
are now labelled and point at the ADR.

**Three of its rules were settled on review (2026-09-13), leaving only the
implementation open:**

- ⚠ **`node_number` / hostname uniqueness stays GLOBAL — but as a REQUIREMENT,
  not as a consequence.** The distinction is the whole value here. The old
  justification ("all clusters share the DMZ/Ceph subnets and the vmid space")
  expires at site #2, which would make a correct guard look wrong and invite
  someone to loosen it. The rule is kept on new footing: a node may move to
  another Proxmox node, another site, or a completely separate network, and a
  globally unique number means such a move is never an identity change. The
  assert is untouched; only its reason changed, in both places that stated it.
- ⚠ **`cloudflare_api_token` is ZONE-scoped, not fleet-scoped.** `vars.yml`
  undersold it as forkable "if per-cluster revocation is wanted" — it is a
  **correctness** constraint: the token is restricted to specific zone
  resources, so a token for zone A cannot solve DNS-01 for zone B. It is one
  scope with `base_domain` and they must fork together, or a cluster can name a
  hostname it cannot get a certificate for. Subdomains of one zone keep a single
  token serving everything.
- **Vaults are for access, suffixes are for description.** Considered using
  per-site vaults as the namespace (bare `proxmox_api_host` in each) and
  rejected it for now: the control node provisions *every* site, so there is no
  access boundary to draw, and a per-site vault would be a namespace wearing an
  access-control costume. It would also fork ADR-0033's flat `secrets` contract
  into two shapes. ⚠ Revisit if a scoped-per-site operator ever becomes real —
  that would make sites a genuine boundary, exactly as clusters are for ESO.

The reassuring half: the *store* needs no new machinery. Field labels are a flat
global namespace suffixed by owning scope (`proxmox_api_host_<site>` alongside
`k3s_token_<cluster>`), items are organisational only — ⚠ they cannot be the
namespace, because labels must be globally unique across items — so the loader,
the broker and `secret_names` are untouched. That falls straight out of
ADR-0033's flat `secrets` shape.

**Open, and it is a subscription question rather than an engineering one:**
1Password's daily cap is **per account, shared across every service account** —
1,000/24h on Individual/Families. Ansible is nowhere near it (~4 calls per
`site.yml`). ESO would be: ~480/day at the default `1h` refresh with 20
`ExternalSecret`s, and per external-secrets#4925 an invalid token can exhaust a
*daily* quota at retry cadence in ~20 minutes. Set `refreshInterval`
deliberately at that milestone, and re-evaluate the plan tier.

---

## 2026-09-12 — Sealed the secrets broker: `secret_names` published, `bws.*` gone from call sites, fact renamed to `secrets`

**Related:** [ADR-0033](decisions/0033-secrets-fact-broker.md) (now Accepted, verified) ·
[ADR-0027](decisions/0027-control-node-secrets-bws-runtime.md) ·
[ADR-0026](decisions/0026-per-cluster-derivation-from-index.md) ·
Code: `ansible/playbooks/tasks/load-secrets.yml`,
`ansible/inventory/group_vars/all/vars.yml`, `ansible/inventory/hosts.yml`,
`ansible/roles/flatcar_vm/tasks/preflight.yml`,
`ansible/playbooks/render-ceph-setup.yml`.

Implemented as argued, in two commits — the seal, then the rename — because a
~40-site mechanical rename folded into a behavioural change is an unreviewable
diff.

**The seal.** `secret_names` is published from the `names` list the module was
already returning and already printing. The two *structural* sites were rewritten
against it and now read no secret value at all; the two plain leaks
(`proxmox_ssh_addr`, `proxmox_ssh_user`) got brokered variables.

⚠ **One thing the ADR did not anticipate.** `render-ceph-setup.yml`'s CephFS-name
assert could not simply move to the brokered `ceph_csi.fs_name`: reading *any*
key of `ceph_csi` templates the whole dict, and on a first run `ceph_csi.mons`
calls `.splitlines()` on an absent secret and raises a Jinja error in place of
the carefully-worded `fail_msg` — the very trap the original `bws.ceph_fs_name`
dereference existed to dodge. Fixed with a standalone `ceph_fs_name` broker
(one reference to the secret, `ceph_csi.fs_name` sourced from it). The assert
also relies on `assert` short-circuiting on the first false condition, so the
presence test must be listed *before* the length test; that ordering is now a
comment at the site, because it is invisible otherwise.

**Verification** (live store, 29 secrets):

| Check | Result |
|---|---|
| `render-frr-config.yml` + `render-ceph-setup.yml` byte-identical to a pre-change baseline | pass, after the seal **and** again after the rename; `changed=0` both times |
| `k3s_tls_sans_*` assert still fires | pass — poisoned with a `k3s_tls_sans_ghost` name via `-e`, failed naming `ghost`, aborted in preflight before any Proxmox call |
| new name-list gates vs the old dict gates | identical on every secret; `secret_names` sorted and equal to `secrets.keys()` |
| `--syntax-check` on all seven playbooks | pass |

**Lesson, and it cost a failed run.** The rename swept `bws.`, `` `bws` ``,
`bws[` and `) in bws` — but not the `set_fact` **key** `bws:` itself, which is
the one place the name is defined rather than used. Every consumer moved to
`secrets` while the fact was still published as `bws`, and the whole repo failed
with `'secrets' is undefined`. A rename regex that covers every *reference* and
misses the *definition* fails 100% of the time, which is the good case — caught
by simply running the safest play. The byte-identical baseline is what made
"caught immediately" possible.

**Not done here, deliberately:** the `bws_*` connection variables, the
`bws_secrets` module and the `bws_fetch` register still name Bitwarden. They
describe the vendor connection, and go with the store rather than with this
record — so `grep -rn 'bws' ansible/` is expected to be non-empty until it does.

---

## 2026-09-12 — Explored replacing BWS with 1Password; found two claims already wrong

**Related:** [ADR-0032](decisions/0032-secrets-store-1password-migration.md) (Open) ·
[ADR-0033](decisions/0033-secrets-fact-broker.md) (Proposed) ·
[ADR-0027](decisions/0027-control-node-secrets-bws-runtime.md) ·
[ADR-0009](decisions/0009-secrets-aescbc-and-eso-bitwarden.md) ·
[ADR-0021](decisions/0021-topology-blinding-postbuild-substitution.md)

No migration performed and none decided. Recorded as an Open ADR because the
investigation turned up things that outlive the question, and because the
answer gets more expensive after the ESO milestone rather than before.

**Two corrections landed, both true regardless of any store change:**

| Found | Fix |
|---|---|
| `tasks/load-bws-secrets.yml` claimed Keychain unlock "is Touch ID rather than a typed passphrase". `vars.yml` says the opposite, in detail, and is right — `security` has no biometry flag. | Comment rewritten, pointing at the authoritative note. |
| The "ESO cannot be pulled earlier" chain is written as SDK Server → cert → **Gateway → LoadBalancer IP → BGP**. The last two links do not belong: this cluster issues by **DNS-01**, which solves with no inbound path. Gateway and LB IP are what *serving* the wildcard needs, not *issuing* it. | Reference text corrected in root `CLAUDE.md` and `architecture.md` §3.6. ADR-0009/0021 left standing — records are not edited to change the past; ADR-0032 carries the correction. |

The second one also cost `cluster-topology`'s stated justification, so the
reference text now gives the two reasons that actually hold: a tier applies in
**one pass with no intra-tier ordering**, so a Secret produced by a controller
inside `infrastructure` can never be a `postBuild` source for `infrastructure`;
and Calico is Ansible-primed from the same values before Flux exists (ADR-0016).
The conclusion — permanently Ansible-seeded — was never in doubt; only the
argument for it was wrong.

**On the swap itself.** `feat/eso` was zero commits ahead of `main` with a
clean tree, so the cluster half is greenfield — not a migration, just a
different choice at an unstarted milestone, and the cheaper one there (the
1Password SDK provider runs **no in-cluster server**, so no Deployment,
Service, `Certificate` or image digest). The control-node half is ~3–4 days,
more than half of it prose.

⚠ The finding that would decide it: 1Password's daily rate limit is **per
account, shared across every service account** — 1,000/24h on
Individual/Families. Ansible fits easily (~20 calls per `site.yml` with grouped
items). ESO does not: ~480/day at the default refresh with 20 `ExternalSecret`s,
and per external-secrets#4925 an invalid token consumes quota at retry cadence
and can exhaust a **daily** limit in ~20 minutes — locking out ESO *and* the
control node, including the playbook that would fix it. Below Teams this is a
regression against BWS's undocumented-but-burst-shaped throttling.

One genuine reversal is available if it proceeds: ADR-0027 rejected grouping
secrets because *"a BWS secret has no fields"*. 1Password items have typed,
labelled fields, so that premise is simply void — grouping by purpose becomes
correct rather than a JSON hack, and it is also what keeps the call count down.

**ADR-0033 split out deliberately.** Grepping for consumers showed the `bws`
fact escaping its `vars.yml` broker in four files. Two are ordinary omissions;
two are **structural** — `preflight.yml`'s `k3s_tls_sans_*` guard and
`render-ceph-setup.yml`'s presence checks ask questions about the *set of
secret names*, which a brokered value cannot express, so there is no correct
way to write them today. The module already returns a safe-to-log `names` list
that is printed but never published as a fact. Publishing it as `secret_names`
fixes both, and drops those tasks out of the `no_log` blast radius of a
structure whose values they never read. Correct either way, so it is not
buried inside the contested decision.

---

## 2026-09-07 — ceph-csi live against the Proxmox Ceph: both StorageClasses provisioning, RBD on krbd

**Related:** [ADR-0006](decisions/0006-ceph-csi-external-proxmox-ceph.md) ·
[ADR-0031](decisions/0031-ceph-csi-operator-vendored-manifests-not-helm.md) ·
[`proxmox-ceph-k8s-setup.md`](proxmox-ceph-k8s-setup.md) ·
`gitops/{crds,infrastructure,infrastructure-config}/ceph-csi*` · commit `0d3b44c` (#16)

ceph-csi-operator **v1.0.4** against the existing Proxmox Ceph (**Squid
19.2.3**), with `ceph-rbd` (RWO, **cluster default**) and `cephfs` (RWX). The
last infrastructure controller before ESO.

The Ceph side is a new kind of artifact here: a generated-then-run-by-hand
procedure ([`proxmox-ceph-k8s-setup.md`](proxmox-ceph-k8s-setup.md) +
`render-ceph-setup.yml`), the same shape as the pfSense/FRR runbook and for the
same reason — live production gear with no safe automation path. Concretely:
`ceph` reads `/etc/pve/ceph.conf` out of pmxcfs (root-only), the `provisioner`
SSH user's sudo is scoped to `qm`, and **cephx user creation is not in the
Proxmox API at all**.

| Evidence | |
|---|---|
| Ceph side | `ceph-k8s-verify.sh` **16/16 PASS** on `phoenix-1`: pool `k8s-rbd` size 3/min_size 2/app rbd, subvolumegroup `k8s` at `/volumes/k8s`, all 7 caps on both users, plus the negative check (RBD osd cap pool-scoped, no wildcard) |
| Guardrail | `k8s-rbd` is **not** registered as a PVE storage — Proxmox cannot place VM disks in the Kubernetes pool |
| Path | 3 mons reachable on msgr1+msgr2; **jumbo 8996 end-to-end, DF, unfragmented**; `rbd.ko`/`ceph.ko`/`libceph.ko` present on Flatcar 6.12.102 |
| Source | `SourceVerified=True` on `latest@sha256:bfe98a5b…`; all 5 CRDs **Established** via server-side apply |
| Drivers | operator + both ctrlplugins (4/4) + both nodeplugins (2/2) Running |
| **RBD PVC** | Bound in ~1 s **without naming a class** (proving the default annotation); mounted `/dev/rbd0 … type ext4` — the **kernel** client mapped it |
| **CephFS PVC** | Bound RWX; mounted `type ceph`, `name=k8s-cephfs`, path `/volumes/k8s/csi-vol-…`, `mon_addr=` all three mons on the Ceph public net |
| Lifecycle | Wrote and read back on both, then deleted: **no orphaned PVs** — `reclaimPolicy: Delete` works on both classes |

**`imageFeatures: layering` is now proven, not assumed.** ADR-0006 carried "RBD
image features vs the Flatcar kernel" as an untested risk since July. The mount
line `/dev/rbd0 … type ext4` is the proof: krbd — not fuse, not nbd — mapped the
image. krbd supports neither `object-map`, `fast-diff` nor `deep-flatten`, and
an image created with them fails at **attach on the node**, long after the PVC
looks healthy.

**Four things worth carrying forward:**

1. **The operator's Helm chart cannot be installed by Flux at all** (ADR-0031).
   Its `operatorconfig` (536 KB) and `driver` (505 KB) CRD templates are each
   ~2× the 262144-byte client-side apply limit, helm-controller can't apply
   server-side, and — unlike Calico's and cert-manager's charts — there is **no
   `crds.enabled` toggle**: both templates are unconditional, the only
   expression in either being a labels helper. Vendored manifests instead. That
   makes ceph-csi the **third** occupant of the `crds/` tier; three independent
   charts have now hit the same wall, so that tier is load-bearing
   infrastructure rather than a Calico-specific workaround.
2. **⚠ `wait: true` on `infrastructure-config` does NOT gate on the drivers
   running** — this corrects a claim made while building it. `Driver`,
   `ClientProfile` and `CephConnection` all expose an **empty status**, and
   kstatus treats a statusless resource as Current immediately, so the tier went
   Ready while the plugin pods were still `ContainerCreating`. **Consequence:
   `apps` does not gate on storage being functional**, which is *not* what the
   tier's cert-manager precedent implies — a `Certificate` carries a real Ready
   condition, so there the gate genuinely means "issuance worked". Anything that
   must not start before storage works needs its own check (a health check on
   the Kustomization, or an init container).
3. **`monitors` is a YAML list while post-build substitution is string-level** —
   the "substitution can't go here" case in `gitops/CLAUDE.md`. Solved without
   SOPS and without baking in a mon count: `cluster-topology` carries
   `ceph_mons_yaml`, a single value that is already a complete JSON array (a
   valid YAML flow sequence), substituted whole. The `to_json` quoting is
   load-bearing — an unquoted `host:port` is not a valid scalar in flow context.
4. **RWX shares PVE's existing CephFS under its own subvolumegroup** rather than
   adding a second filesystem, which would need a second *active* MDS and spend
   the standby redundancy PVE's production filesystem relies on (1 active + 2
   standby, confirmed). Two cephx users, not one: `client.k8s-cephfs` needs
   `mgr "allow rw"` because subvolume operations run through the mgr `volumes`
   module and Ceph ships no narrower profile — splitting keeps that cap off the
   RBD credential guarding Postgres/Qdrant/Redis.

**Two verification traps, both of which produced a confident wrong answer:**

- **Flatcar's bash has no `/dev/tcp`** (built without net-redirections), so a
  port check written that way reports **every** port closed — including the k3s
  API, which was open. Caught only by testing a known-open port as a control.
  Use `curl telnet://host:port`.
- **`ceph fs status` never prints the string `up:standby`** — it renders a human
  table whose STATE column says `active`, and lists standbys under a `STANDBY
  MDS` heading. `ceph-k8s-verify.sh` grepped for `up:standby` and so reported
  "0 standby" on a cluster that had two, which would have argued for exactly the
  wrong CephFS layout. Fixed to read `ceph fs dump -f json` `.standbys`, and
  promoted from an informational line to a check that fails at zero.

Also: the control node's **macOS Local Network Privacy** grant lapsed mid-work,
blocking venv Python from the on-link PVE API while Apple-signed `curl`
connected in 7 ms. The three-legged A/B is what settles it — the *same* Python
process reached the **routed** k3s API fine and failed only on the same-link
host (`ansible/README.md`).

**Next:** ESO + Bitwarden SDK Server, where the bootstrap-seeded-Secret adoption
question gets decided (`decisions/README.md`, "Open questions"). Note ceph-csi's
two cephx Secrets are **not** upstream of ESO the way cert-manager's token is:
ESO and the Bitwarden SDK Server are both stateless, so nothing about ESO needs
a StorageClass. **ceph-csi before ESO is a choice, not a dependency** — see that
open question for why the order is worth keeping.

## 2026-09-05 — cert-manager + the wildcard cert: ADR-0013's cert half done, HTTPS live on the Gateway

**Related:** [ADR-0013](decisions/0013-ingress-certs-dns-external-access.md) ·
`gitops/infrastructure/cert-manager/` · `gitops/infrastructure-config/` ·
commits `197cc04` (#12), `742019f` (#13)

cert-manager **v1.21.1** (OCI chart), Let's Encrypt prod + staging
ClusterIssuers (Cloudflare DNS-01), and the `*.${base_domain}` wildcard —
plus a **new `infrastructure-config` tier** for CRs whose CRDs arrive with
their controller's chart (same-pass apply fails). The chain is now
`crds → infrastructure → infrastructure-config → apps`, and that tier's
`wait: true` makes tier-Ready mean *the cert actually issued*. Ansible seeds
`base_domain` into `cluster-topology` and the `cloudflare-api-token` Secret
(bootstrap tier — upstream of ESO; the adoption question stays open).

| Evidence | |
|---|---|
| HelmRelease | cert-manager v1.21.1 install → later upgrade (#13) both **succeeded**; controller rolled |
| Issuers | `letsencrypt` + `letsencrypt-staging` both Ready — ACME accounts registered (no email, deliberately: optional in ACME, LE ended expiry mail in 2025, keeps personal data out of a public repo) |
| Certificate | `wildcard` **Ready**; issuer Let's Encrypt (YR1), subject `*.<domain>`, notAfter 2026-12-04 |
| Gateway | `https` listener `ResolvedRefs=True, Programmed=True`; `:80` narrowed to the redirect route only |
| Endpoint | `curl --resolve` → **404 over a valid LE chain** (`ssl_verify_result: 0`, no `-k`) in 0.17 s; `:80` → **301** to https |
| Tiers | all five Ready at the same digest (`7aaebd94…`), #13's resolver flags verified on the deployment |

**Three catches, each with a lesson:**

1. **The Cloudflare token had account-wide `DNS:Edit` (4 zones)** where a
   single zone was intended — caught by preflight (TXT write on a NON-target
   zone succeeded), fixed by editing the token in place (same secret value, no
   reseed). Lessons: `/user/tokens/verify` answers `Invalid API Token` for
   **account-owned** tokens — a false negative; verify with `GET /zones` and a
   TXT round-trip instead. And account-owned tokens are edited under *Manage
   Account → Account API Tokens*, not the profile page — editing the
   similarly-named profile token silently changes nothing.
2. **Issuance stalled ~30 min on "not yet propagated"** while the TXT was live
   publicly. Two stacked causes: pfSense's **DNS-redirect NAT** sends port-53
   traffic for any destination outside the `OkayDnsServers` alias to PiHole —
   *answering as the destination's address*, so the self-check believed it had
   asked 1.1.1.1 — and the zone's SOA negative TTL (1799 s) kept the
   pre-record NODATA alive. Fix (#13, permanent): `dns01RecursiveNameservers`
   = 1.1.1.1/8.8.8.8 + `...Only=true` — the self-check must ask what LE asks,
   *especially* once split-horizon DNS diverges the internal view forever.
   Those resolvers must be in the `OkayDnsServers` alias, and the only honest
   verification is a CHAOS-class `whoami.cloudflare` query from a cluster pod
   (answered = genuine; a plain lookup cannot detect the hijack).
3. The cert actually issued through the redirect path once the negative cache
   expired — *before* #13 rolled. Working ≠ correct: without #13 every ~60-day
   renewal replays the same race.

**Next:** ceph-csi-operator + StorageClasses (then ESO, where the
secret-adoption open question gets decided).

## 2026-09-05 — NGINX Gateway Fabric deployed; the first Flux-only delivery, verified

**Related:** [ADR-0013](decisions/0013-ingress-certs-dns-external-access.md) ·
[ADR-0020](decisions/0020-crd-tier-vendored-server-side-apply.md) ·
[ADR-0024](decisions/0024-calico-ebpf-dataplane-no-kube-proxy.md) ·
`gitops/infrastructure/nginx-gateway-fabric/` · commit `2b2f12a` (#9)

First delivery through the pure GitOps path — merge → CI signs → Flux applies —
with **no Ansible priming anywhere**. Verified against the live cluster within
minutes of the merge (source poll nudged with a `reconcile.fluxcd.io/requestedAt`
annotation rather than waiting out the 10m interval):

| Evidence | |
|---|---|
| Source | `SourceVerified=True` on `latest@sha256:3d08dd4f…`; all four tiers Ready at that digest |
| CRD tier | all 8 `gateway.networking.k8s.io` CRDs **Established** (Gateway API v1.5.1 standard bundle, vendored from NGF's repo — `httproutes` ~431 KB exceeds the client-side apply limit, same admission rule as Calico) |
| HelmRelease | NGF **2.6.7** install succeeded, via `OCIRepository` + `chartRef` (NGF publishes charts only to OCI — first use of that pattern here) |
| Gateway | GatewayClass `nginx` **Accepted**; Gateway **Accepted + Programmed** — NGF 2.x provisions the data plane *per Gateway*, so creating it is what spun up the `gateway-nginx` Deployment + LoadBalancer Service |
| LB allocation | `EXTERNAL-IP` assigned from `${lb_range}` immediately — the #12890 workaround doing its job |
| Reachability | `HTTP 404` from nginx in ~9 ms, curled from the control Mac on a **different segment** — the BGP-advertised route carries real traffic |
| Source IP | the nginx access log shows the Mac's **real address** (`x.x.x.N` on its own segment, not a node IP) under `externalTrafficPolicy: Cluster` — ADR-0024's source-IP claim now proven at the Gateway itself |

Two notes for later readers:

- The chart's `externalTrafficPolicy` default is `Local`; we override to
  `Cluster` deliberately. Under the eBPF dataplane **both** policies preserve
  the client IP — the difference is load distribution: `Cluster` spreads across
  every endpoint cluster-wide, while `Local` balances **per node** (pfSense
  ECMP over `/32`s), which skews with uneven scheduling and can briefly
  blackhole during rollouts until the `/32` withdraws. Pinning traffic to
  backend-holding nodes is `Local`'s only remaining legitimate use. Academic on
  today's single node; the precedent is what matters.
- Upstream's `safe-upgrades` ValidatingAdmissionPolicy (ships in the CRD
  bundle) is kept — it refuses CRD downgrades and standard→experimental channel
  swaps.

**Next:** cert-manager (DNS-01 wildcard), then the HTTPS listener + wildcard
cert on this Gateway.

## 2026-08-30 — From-scratch `site.yml` run verified; the 1→2→4→3 sequence is closed

**Related:** [ADR-0028](decisions/0028-gitops-delivery-signed-oci-syncless-fluxinstance.md) ·
[ADR-0018](decisions/0018-calico-bgp-replaces-metallb.md) · `ansible/site.yml` ·
commit `5bf72b1` (#6)

This was the one part of the Flux-delivery milestone's DoD still outstanding —
it had been blocked on the macOS Local Network Privacy grant (see the 2026-08-29
TCC entry below). The grant landed and the full run — provision →
bootstrap-cluster → flux-bootstrap — completed clean. Verified against the live
cluster after the run, **not** inferred from a zero-exit:

| Evidence | |
|---|---|
| Fresh node | `snoop-a2o` **Ready**, `v1.36.2+k3s1`, ~7 min old — a genuine rebuild, no prior-run residue |
| Calico | every `tigerastatus` **Available**; all `calico-system` pods Running |
| BGP dataplane | session to the pfSense peer **Established**; `BGPPeer.peerIP` **resolved** (not `""`), so StrictPostBuildSubstitutions worked |
| #12890 workaround | `calico-kube-controllers-ipamconfigs-workaround` ClusterRole present, seeded at bootstrap |
| Flux | operator + `FluxInstance` **Ready**, Flux **v2.9.4** |
| **Step 3: OCI source** | Flux source is `OCIRepository/flux-system` (`oci://ghcr.io/nighlabs/homelab-infra/gitops`); **zero `GitRepository` exists** — sync-less confirmed |
| **Step 3: cosign verify** | `SourceVerified=True :: verified signature of revision latest@sha256:ff22…` — the artifact's keyless signature is checked and passing |
| Adoption, not collision | the seeded `OCIRepository` and `BGPPeer` both carry `kustomize.toolkit.fluxcd.io/name` labels — Flux drift-corrects the very objects Ansible seeded |
| All tiers | `crds`/`infrastructure`/`apps`/`flux-system` **Ready** at the same OCI digest |

The load-bearing row is `SourceVerified=True` on an OCI source with **no**
`GitRepository`: that is step 3's entire thesis — Flux pulling the
keyless-cosign-signed artifact and verifying it before applying — proven on a
clean provision. **Steps 4 and 3 are both done; the 1→2→4→3 sequence is
closed.**

⚠ **One hygiene fix rode in with this run** (`roles/flatcar_vm/tasks/main.yml`):
a leftover `vault_proxmox_ssh_user` → `bws.proxmox_ssh_user`, missed in the
2026-08-17 vault→BWS retirement. It lives inside an assert's `fail_msg`, so a
passing run never renders it — the clean run did **not** validate the fix, it's
correctness for the day that snippet-dir assert actually fires.

**Next milestone:** deliver the rest of the stack from Git (Gateway →
cert-manager → ceph-csi → ESO → …). ⚠ Do NOT fold "stop vendoring the Calico
CRDs" into it — `bootstrap-cluster.yml` primes from the vendored file, so that's
its own step (see [ADR-0020](decisions/0020-crd-tier-vendored-server-side-apply.md)).

## 2026-08-30 — Step 3 merged: sync-less FluxInstance, committed `OCIRepository` + root `Kustomization`

**Related:** [ADR-0028](decisions/0028-gitops-delivery-signed-oci-syncless-fluxinstance.md) ·
`gitops/deployment/homelab/source.yaml`, `sync.yaml` · commit `de2a588` (#4)

Step 3's shape was decided 2026-08-29, implemented on branch
`step3-oci-flux-source`, merged to `main` as #4, and verified live on the
from-scratch run above. What changed:

- The FluxInstance is now **sync-less** (`spec.sync` cannot express `verify`).
  Ansible seeds a committed `OCIRepository` (`source.yaml`, cosign `spec.verify` +
  `matchOIDCIdentity`) + root `Kustomization` (`sync.yaml`), both inside the path
  they reconcile so Flux adopts and drift-corrects them.
- The three tier entrypoints point at `OCIRepository/flux-system` with the
  `./gitops` prefix stripped from every path (`./crds`, `./infrastructure`,
  `./apps`; root sync path `./deployment/homelab`). ⚠ **The artifact root is
  `gitops/` itself**, so the prefix the GitRepository source required is gone.
  The failure this prevents — source Ready, Kustomization failing "kustomization
  path not found" — is the one already burned into this repo's history (Failure 2,
  2026-08-29), so keep the prefix off any new tier added under the OCI source.
- **Phase 2 of `flux-bootstrap.yml` is gone** along with its `when: not exists`
  guard — with no `spec.sync` there is nothing for a re-run to strip. The
  two-pass design described in the 2026-08-29 entry below is superseded.
- ⚠ **Two migration facts:** (1) the OCI artifact must be **rebuilt from `main`**
  before the play runs, since Flux pulls the artifact, not the branch; (2) the
  play now **REFUSES to convert a live sync-based FluxInstance in place** (that
  would cascade-prune Calico) — migration is by **re-provision**, per the
  disposable-cluster rule.

## 2026-08-30 — BWS organization id read from the Keychain

**Related:** [ADR-0027](decisions/0027-control-node-secrets-bws-runtime.md) ·
`ansible/BWS-SECRETS.md` · commit `24b15f3` (#5)

The org id now comes from a `BWS_ORG_ID` Keychain item by default, the same way
as the access token. `export BWS_ORG_ID=…` takes precedence over the Keychain
item; `-e bws_organization_id=…` beats both. Unlike the token, a *missing*
org-id Keychain item is not an error — it falls through to the env/`-e` path
(the id isn't secret, only environment-identifying).

⚠ **It is the ORGANIZATION uuid, not the project uuid.** Getting it wrong fails
as **`404 Resource not found` on `sync()`** *after* authentication succeeds —
which is the tell: auth working but the call 404ing means the org id, not the
token. A permissions problem would be `403`. Hit for real during the migration;
the module now says so in the error.

## 2026-08-29 — Step 4 verified: the gitops OCI artifact is built, signed, and public

**Related:** [ADR-0028](decisions/0028-gitops-delivery-signed-oci-syncless-fluxinstance.md) ·
`.github/workflows/gitops-artifact.yml` · commits `a0f4434`, `ea3bf7f`, `3b0f838`

`.github/workflows/gitops-artifact.yml` publishes and keyless-cosign-signs
`ghcr.io/nighlabs/homelab-infra/gitops`. Verified independently, not just "the
job went green": `cosign verify` against
`--certificate-identity-regexp='^https://github.com/nighlabs/homelab-infra/'` +
the GitHub OIDC issuer passes (claims validated, transparency-log entry
confirmed, cert chained to a trusted CA), and the pulled layer contains all 21
manifests and **zero markdown**. The package is **public** (anonymous pull
works), so **no image pull secret is needed** — one less bootstrap-tier secret
than expected.

- ⚠⚠ **The artifact root is `gitops/` itself — the prefix is gone.** Paths
  inside it are `deployment/homelab/…`, `infrastructure/…`, `crds/…`. Every tier
  path had to lose its `./gitops` prefix at step 3.
- ⚠ `--reproducible` stabilises the LAYER digest, not the manifest digest —
  `org.opencontainers.image.revision` embeds the commit SHA, so every build mints
  a new manifest digest and therefore a new OCIRepository revision. That is why
  the workflow negates `gitops/**/*.md` in its trigger `paths` rather than
  relying on the ignore list alone.
- `--ignore-paths` had to be fixed for the pinned flux v2.9.4 (`ea3bf7f`).

## 2026-08-29 — GitOps delivery decided: cosign-signed OCI artifact, sync-less FluxInstance

**Related:** [ADR-0028](decisions/0028-gitops-delivery-signed-oci-syncless-fluxinstance.md) ·
commit `dcb927b`

The decision, its alternatives (`spec.sync` — no `verify` field; patching the
generated source — reverted by the operator's inventory; GitHub attestations —
not a valid `verify.provider`; a long-lived cosign key — one more secret), and
its accepted consequences are in the ADR. The implementation was sequenced
**1→2→4→3**: (1) Calico BGP, (2) Flux bootstrap, (4) the artifact workflow,
(3) rewiring Flux to the artifact — step 4 before step 3 because Flux pulls the
artifact, not the branch, so the artifact has to exist before the source can be
pointed at it.

## 2026-08-29 — Flux bootstrap verified live: the adoption is proven

**Related:** [ADR-0008](decisions/0008-flux-via-flux-operator.md) ·
[ADR-0016](decisions/0016-calico-ansible-primes-flux-adopts.md) ·
`ansible/playbooks/flux-bootstrap.yml`, `playbooks/tasks/flux-bootstrap-cluster.yml` ·
commits `585adf8`, `483a4b8`, `532411b`

`playbooks/flux-bootstrap.yml` + `playbooks/tasks/flux-bootstrap-cluster.yml`,
wired into `site.yml` as the fourth import. Proven on a **rolled-back
`pre-flux-adoption` snapshot**, i.e. a genuine first bootstrap rather than a
re-run — the Flux CRDs were absent (`NotFound`, not merely empty) before the play
started, so phase 1 actually ran. `ok=25 changed=3 failed=0`.

| Evidence | |
|---|---|
| Adoption, not collision | helm `v1` **superseded** → `v2` **deployed**, message *"Helm upgrade succeeded"* — helm-controller UPGRADED the CLI-installed release |
| No diff war | `Installation.spec` **byte-identical** to the pre-run snapshot |
| No churn | **zero** pod restart deltas across all namespaces; no baseline pod deleted |
| Ownership moved | `BGPPeer` now carries `kustomize.toolkit.fluxcd.io/name=infrastructure` |
| All tiers | `crds`/`infrastructure`/`apps`/`flux-system` **Ready** at `d8d090d` |
| Substitution resolved | `peerIP=${bgp_peer_ip}`, `clusterASN=64601`, `lb=${lb_range}` — real values, not `""` |
| **Dataplane intact** | test Service got **`x.x.x.131`** and returned **`HTTP 200` in 8.7 ms** from off-segment, routed via pfSense BGP |

The last row is the one that matters: allocation proves Calico's LB IPAM and the
#12890 workaround survived the handover, and reachability proves the BGP session
and prefix lists did too. Flux running **v2.9.4**.

**The two first-run failures below were both caught BEFORE Git sync**, which is
the phase split earning its keep — a failed bootstrap left the cluster provably
untouched both times. Pins: **flux-operator chart `0.58.0`** (⚠ no leading `v`;
the OCI chart tags are bare semver while the GitHub release is `v0.58.0`)
installing **Flux `2.9.x`** — minor pin, patches automatic, exactly the k3s
sysupdate posture.

**The design in one line (as it stood that day): helm-install the operator,
apply ONE `FluxInstance`, let it generate the `flux-system` GitRepository +
Kustomization that the already-committed `gitops/deployment/<cluster>/`
entrypoints reference.** *(Superseded 2026-08-30 by the sync-less design — see
the step 3 entry above. Kept here as the record of what ran on this date.)*

**❌ Failure 1 — the gate assert was wrong, not the cluster.** The operator
already emits `--feature-gates=ObjectLevelWorkloadIdentity=false`, so appending
ours produced two `--feature-gates` arguments and the assert (`length == 1`)
failed the run. **The premise behind it — "a repeated flag overrides, last one
wins" — is FALSE.** `--feature-gates` is a component-base `MapStringBool`: the
first `Set()` clears defaults, later ones **MERGE**. kustomize-controller's own
startup log settles it, logging `loading feature gate` for *both*. So appending
is correct and version-proof, and the assert now folds every `--feature-gates`
argument into an effective map (last writer wins *per key*) and checks the
resulting value — exercised against six cases incl. one-combined-flag and
same-key-twice.

**❌ Failure 2 — FLUX READS THE REMOTE, NOT YOUR WORKING TREE.** With the gate
fixed, phase 2 landed and then sat for 10 minutes: `kustomization path not
found: .../gitops/deployment/homelab`. The rename to `homelab` and the new
`kustomization.yaml` were **committed nowhere** — `origin/main` still had
`snoop-a2o`. Every local check sailed past it, because `kubectl kustomize
gitops/deployment/homelab` proves the build works ON DISK and says nothing about
the branch Flux clones. ⚠ **The symptom is deliberately misleading: the
GitRepository goes READY** (the clone worked, the repo is fine) and only the
`flux-system` Kustomization fails. **A preflight now fetches the remote and
asserts the sync path exists there before anything is applied**, plus warns when
`gitops/` has uncommitted changes Flux cannot see. Ten minutes of retries became
an instant, named failure.

Three further things in the play were non-obvious and load-bearing at the time:
- **The FluxInstance was applied in TWO passes, phase 1 with NO `spec.sync`** —
  this is what made a failed first run harmless. *(Removed 2026-08-30 with the
  sync-less design; the phase split's benefit is now structural.)*
- **Phase 1 was `when: not exists`, and that was NOT an optimization.**
  `apply: true` prunes absent fields, so running it unconditionally would strip
  `spec.sync` from an established instance — deleting the `flux-system`
  Kustomization, whose `prune: true` cascades to all three tiers. *(Also gone
  with `spec.sync`.)*
- **`gitops/deployment/snoop-a2o/` was RENAMED to `homelab/`** — the cluster
  key, not a node name, which is what lets `flux_sync_path` derive as
  `gitops/deployment/{{ cluster_name }}`. ⚠ Renaming that directory without
  renaming the cluster key is Failure 2 all over again.

**The play needs NO credentials** — no BWS, no keychain prompt. Every value is a
committed constant or comes from the cluster via the kubeconfig.

**Two more bugs were found while writing it, both silent-wrong rather than
loud-broken, and both worth carrying forward:**
- A task-level `vars:` entry referencing `item` does **not** re-resolve per
  iteration under this ansible-core's lazy templating. The placeholder-scanner
  ran over all 18 files and produced an **empty list** — it would have asserted
  "nothing missing" against an empty topology Secret. Fixed by using the repo's
  existing single-expression Jinja-loop idiom (`k3s_tokens`, `cluster_bgp`).
- Keys inside **one** `set_fact` are not resolved in declaration order; an
  earlier key referencing a later sibling fails outright. Hence the kustomize
  patch is inlined rather than held in its own variable.

**Snapshot/teardown note, kept because it will be needed again:** rolling back a
Proxmox snapshot is the RIGHT way to get back to a pre-Flux cluster. Deleting the
FluxInstance is NOT — it prunes the generated `flux-system` Kustomization, whose
`prune: true` cascades to all three tiers, and `infrastructure` pruning its
inventory **uninstalls Calico**. ⚠ The mechanism is the **operator's own
inventory + a `fluxcd.controlplane.io/finalizer`**, NOT Kubernetes
ownerReferences — the generated `GitRepository`/`Kustomization` carry *no*
ownerReferences at all (verified 2026-08-29; both appear in the FluxInstance's
34-entry `status.inventory`). So `kubectl delete --cascade=orphan` does **not**
save you: it defeats GC, and GC is not what is doing the deleting. A rollback
with RAM also preserves the kubeconfig and the `cluster-topology` Secret, so
`bootstrap-cluster.yml` does not need re-running. (Commit `532411b` corrected an
earlier claim that ownerReferences were involved.)

Notes from the gitops side:
- **The operator is Ansible-owned and is NOT primed-for-adoption.** Nothing in
  `gitops/` manages the flux-operator, so unlike Calico there is no second
  writer and no HelmRelease for it. Self-management (a HelmRelease for the
  operator, reconciled by the Flux that operator installed) is a real pattern
  and a real footgun — an in-flight upgrade can delete the controller
  performing it. Adopt it deliberately or not at all; don't drift into it.
- **No pull secret** — the repo is public. If it ever goes private that
  changes, and a BWS secret + `secretRef` are the fix.
- `cluster-topology` is already seeded by `bootstrap-cluster.yml`; `sops-age`
  would be seeded only if SOPS is ever needed.

## 2026-08-29 — Proxmox API blocked on the control node: TCC, not routing

**Related:** `ansible/README.md` → Troubleshooting · commits `9461b93`, `8452ba8`

Proxmox-API plays were blocked on the control node — it was the macOS Local
Network Privacy (TCC) denial, already documented in README troubleshooting and
NOT a new problem. Python gets `[Errno 65] No route to host` on `<pve>:8006`
while Apple-signed binaries sail through (`nc` succeeds, `curl` returns **401**
— i.e. Proxmox is reachable and answering). Fix: grant **Zed** (`dev.zed.Zed`,
the shell's owning app) Local Network access, then fully ⌘Q and relaunch.
`flux-bootstrap.yml` and `bootstrap-cluster.yml` were unaffected — they reach
the DMZ, which is *routed* and therefore not gated.

⚠ **This was first misdiagnosed as "the management subnet isn't routable", and
the lesson is about the test, not the fix. LNP gates ON-LINK traffic only.** The
control node is `x.x.x.220/24` and PVE is `x.x.x.21` — same link, so gated. The
k3s node `x.x.x.50` is *routed* via the gateway, so reaching it from Python
exercises nothing and proves nothing. Reasoning "Python reached a LAN host,
therefore the grant is present" is vacuous unless that host is on-link. The only
valid test is **Python vs an Apple-signed binary against the same on-link host
and a port known to listen** — see the README section, which had it right the
whole time. It cost an hour and produced a confident wrong diagnosis. Resolved
2026-08-30 when the grant landed; kept as the diagnostic record because the
failure mode recurs after any move to a new shell app or a TCC reset.

## 2026-08-17 — Secrets migrated to Bitwarden Secrets Manager; `vault.yml` retired

**Related:** [ADR-0027](decisions/0027-control-node-secrets-bws-runtime.md) ·
`ansible/BWS-SECRETS.md`, `ansible/library/bws_secrets.py` · commits `716a2f9`,
`aa7834f`, `fb79e66`, `a6af03a`, `bafde26`

Ansible now reads BWS at run time (one bulk API call), secret zero is a macOS
Keychain item, and nothing secret remains in the repo directory. Manifest of
what exists in BWS: `ansible/BWS-SECRETS.md`.

Two contradicting statements were reconciled in the process — the design doc
said Ansible Vault was the root of trust, the root `CLAUDE.md` later said BWS
was and `vault.yml` a cache. The arrow had reversed and nothing recorded it,
which is precisely how the question got asked again months later.

New/changed: `library/bws_secrets.py` (bulk read) + `library/bws_secret.py`
(create, migration only), `playbooks/tasks/load-bws-secrets.yml` included once
per play, `vars.yml` on `{{ bws.* }}`, `bitwarden-sdk` pinned. The one-shot
port playbook (`port-vault-to-bws.yml`) has been deleted — it did its job.

⚠ **Things that will bite anyone repeating this:** the stock
`bitwarden.secrets.lookup` is UUID-per-call with no name lookup (hence the
custom module); BWS rate limits are undocumented; a BWS secret has NO fields,
so nothing may be stored as JSON; and `security` cannot see the Passwords app
(different keychain), with Keychain Access.app removed in macOS 26.

**Project/account layout:** `homelab-infra` is read by the control node only.
App secrets get a SEPARATE project read by ESO — sharing one would make a
cluster compromise reach the Proxmox token and SSH keys. ⚠ **Cluster-bound ≠
ESO-managed:** the BGP/topology values stay Ansible-seeded as
`cluster-topology` **permanently**, because ESO needs a LoadBalancer IP that
BGP produces.

**§7 item 14 (scoped kubeconfig for Flux) de-scoped as a category error.** The
item used to bundle "should Flux get a scoped ServiceAccount kubeconfig?" with
control-node kubeconfig hygiene and called the two questions "really one". They
were never one: an in-cluster Flux controller never uses a kubeconfig at all —
`spec.kubeConfig` on a Kustomization/HelmRelease exists solely for reconciling a
*remote* cluster; locally each controller authenticates with its own
ServiceAccount token. Nothing about the fetched admin kubeconfig changes what
Flux can do. **Do not re-raise it as a Flux blocker.** The control-node hygiene
half (a cluster-admin cert sitting in `ansible/.kube/`) remains open and cheap
to defer — the file is regenerated on every `bootstrap-cluster.yml` run.

## 2026-08-16 — Calico BGP migration complete and verified on a from-scratch rebuild

**Related:** [ADR-0018](decisions/0018-calico-bgp-replaces-metallb.md) ·
[ADR-0023](decisions/0023-rfc8212-real-policy-le32.md) ·
[ADR-0019](decisions/0019-k3s-1.36-calico-3.32.1-version-pair.md) ·
`gitops/infrastructure/calico-bgp/` · commits `9bbd90c`, `4e4291b`, `a082098`

Proven end-to-end on a **from-scratch rebuild** of `snoop-a2o` (VM destroyed
and reprovisioned, not an in-place change — so no VXLAN residue and the
first-boot path is what got tested). §7 items 6, 8 and 13 closed; item 15's
workaround confirmed working rather than merely applied.

| Evidence | |
|---|---|
| Dataplane | `bgp: Enabled`, `linuxDataplane: BPF`, pod pool **`Never / Never`** (no IPIP, no VXLAN) |
| Session | `Global_172_16_1_1 BGP master up **Established**`, stable |
| Allocation | test Service got `x.x.x.128` immediately (**#12890 workaround works**) |
| **Reachability** | **`HTTP 200` in 10 ms from the LAN segment, hop 1 = pfSense** |
| Export | BIRD exports **exactly** `${lb_range}`; pfSense shows `PfxRcd 1`, `Displayed 1 routes` |
| Pod CIDR containment | **absent from `show ip bgp`** — confirmed on BOTH sides independently |
| Reverse direction | `PfxSnt 0` — pfSense advertises nothing to the cluster |

**The two controls were each independently confirmed**, which is the point of
having them: Calico's BIRD says the pod CIDR is not exported, and pfSense says
it is not received. Either one alone would have been the device we were
guarding against misconfiguring (item 8).

**✅ BOTH advertisement modes exercised — `le 32` is verified, not assumed.**
A first pass only produced the `/24` block (`Cluster` policy), which would have
left the `le 32` prefix-list clause untested. So a second Service was run with
`externalTrafficPolicy: Local` alongside it:

| Service | Policy | Calico exports | pfSense `show ip bgp` |
|---|---|---|---|
| `.129` | `Cluster` | *(no own route — rides the block)* | `${lb_range}` |
| `.130` | `Local` | `x.x.x.130/32` | `x.x.x.130/32` ✅ **accepted** |

The `/32` was **accepted, not dropped** — proving `le 32` does real work. A bare
`permit <lb_range>` would have matched the exact `/24` only and sent that route
to `seq 20 deny any`. Both routes withdrew cleanly on teardown.

⚠ **This is deliberately the one check `curl` cannot make.** At one node the
`/32` is redundant for reachability — `.130` stays reachable via the block
either way — so a broken filter would be **invisible to any connectivity test**
and visible only in the routing table. At two nodes it stops being cosmetic:
the `/32` is what steers traffic to the node actually holding the backend, and
without it ECMP scatters across nodes that have no local pod and `Local` drops
it. Do not "simplify" `le 32` away.

**✅ Source IP preservation re-verified (item 17's whole justification).** Under
`externalTrafficPolicy: Cluster`, the pod logged the real off-cluster client
(`x.x.x.188`), **not** the node address (`x.x.x.50`). With kube-proxy that
SNAT would be unrecoverable at L7. This is why the runbook's §10 now says use
`Cluster`, reversing its original `Local` advice.

**#12890 workaround verified working and necessary.** On the rebuild the
`kubectl auth can-i get ipamconfigs --as=system:serviceaccount:calico-system:calico-kube-controllers`
assert returned `yes`, and LoadBalancer allocation succeeded immediately (no
`pending` phase). The ClusterRole is present as
`calico-kube-controllers-ipamconfigs-workaround`. Note this does NOT prove
upstream is still broken — that check is the removal criterion, i.e. remove the
workaround and see whether `can-i` still says `yes`.

**`assignIPs` left at `AllServices`** — right *only* because Calico is the sole
LB IPAM here; revisit the moment a second LoadBalancer IPAM provider is added
(see `infrastructure/calico-bgp/ippool-loadbalancer.yaml` header and ADR-0018).

**The BGP CRs live in `infrastructure/calico-bgp/`, not `calico/` — forced by
kustomize and verified rather than assumed.** `calico/kustomization.yaml` sets
`namespace: tigera-operator`, and the namespace transformer stamps a namespace
on every resource it can't prove is cluster-scoped (it ships schemas for core
kinds, not CRDs), so `BGPConfiguration`/`BGPPeer`/`BGPFilter`/`IPPool` all
emerged carrying `namespace: tigera-operator`. A JSON6902 `op: remove` on
`/metadata/namespace` errors with *"Unable to remove nonexistent key"* because
patches run before the namespace transformer. A sibling directory outside the
transformer's scope is the robust answer.

## 2026-08-16 — pfSense values decided, config generator written, paste landed

**Related:** [ADR-0026](decisions/0026-per-cluster-derivation-from-index.md) ·
[ADR-0022](decisions/0022-pfsense-frr-raw-config-explicit-neighbors.md) ·
`ansible/playbooks/render-frr-config.yml` · [`pfsense-frr-bgp-setup.md`](pfsense-frr-bgp-setup.md)

**§7 item 6 RESOLVED.** Cluster ASN `64601` (`bgp_asn_base + index`), pfSense AS
`64512`, LB range `<lb_range_base>.<index>.0/24`, peer IP = `dmz_network.gateway`
(no CARP on that interface — confirmed; if that ever changes, `bgp_peer_ip`
becomes its own variable). **Nodes did not move, so nothing was re-provisioned.**
Two new vault vars only (`vault_lb_range_base`, `vault_frr_master_password` —
since migrated to BWS as `lb_range_base`, `frr_master_password`); both ASNs are
cleartext.

`playbooks/render-frr-config.yml` renders the pfSense/FRR config and the
firewall-alias member list from `inventory/nodes.yml` + the secrets — verified
rendering correctly for one and two clusters, and its collision asserts
verified *firing*, not just passing (the item 18 lesson). pfSense CE has no
API, so delivery is still a manual paste, but the content is generated and
asserted. The paste landed with the BGP verification above.

Two real bugs were found in the runbook while doing this, both silent-failure
modes: the LB range must **not** live inside the DMZ subnet (pfSense's
connected route beats an equal-length BGP route on admin distance, and on-subnet
clients ARP for an address nothing answers — Calico does no L2 for LB IPs), and
the prefix list needs **`le 32`** or `Local`-policy Services blackhole.

**REVISED — explicit `neighbor` statements, not `bgp listen range`.** Dynamic
neighbors were the original second reason for raw config. They're incompatible
with giving two clusters distinct ASNs on a shared DMZ subnet: a listen range
maps one prefix to one peer group, and a peer group carries exactly one
`remote-as` **and** one set of prefix lists. Consequences: adding a k3s node
costs one pfSense paste (not per rebuild — node IPs derive from `node_number`);
the BGP neighbor list and the firewall alias are the same list, rendered from
the same node map, so they can't drift.

**Firewall:** the whole LB `/16` supernet was added to the "internal networks"
alias so LB reachability fails closed on isolated VLANs written as
`pass … to ! <HomeNets>` (runbook §6).

## 2026-08-16 — k3s join token, TLS SANs, and version are per-cluster

**Related:** [ADR-0026](decisions/0026-per-cluster-derivation-from-index.md) ·
`ansible/roles/flatcar_vm/tasks/preflight.yml`, `inventory/nodes.yml`

**The join token and TLS SANs are per-CLUSTER, not fleet-wide.** Both are now
maps keyed by the cluster name, resolved from `node.cluster`. The token is the
credential that admits a node to a cluster, so one shared value made a leak from
any cluster a leak for all of them — and k3s derives the datastore
bootstrap-data encryption key from it too. TLS SANs are per-cluster by
construction: a stable API name or VIP belongs to one cluster, and a shared list
puts cluster B's name in cluster A's cert.

**Migration was a no-op for a running node**: move the old `vault_k3s_token`
value under the cluster key and the rendered config is byte-identical, so
nothing was re-provisioned. There is deliberately **no fallback** to the old
variable — a loud assert beats a silent half-migration.

⚠ Those facts are set **unconditionally**, not under `when: k3s_enabled`.
`set_fact` persists across the role's per-node loop, so a `when` would leave a
non-k3s node holding the *previous* node's token — the same
stale-registered-value trap as item 18. **Verified on that exact case** (a bare
VM provisioned after two k3s nodes from different clusters resolves to `''`),
not just on the happy path.

**`k3s_version`/`k3s_minor` are per-cluster too.** `k3s_version_default` in
`group_vars` is the fleet default; a cluster overrides it with `k3s_version:` in
its `inventory/nodes.yml` block, so a minor bump can be staged on one cluster.
**`k3s_minor` is now DERIVED** from the effective version (`v1.36.2+k3s1` →
`v1.36`) instead of stated separately; an explicit override still wins, and
preflight asserts containment on both paths — verified firing on a deliberately
mismatched override. Why that assert matters: if the seeded sysext falls outside
the sysupdate `MatchPattern`, the node boots the *correct* k3s and then silently
never updates. `snoop-a2o` resolves to `v1.36.2+k3s1`/`v1.36` exactly as before.

⚠ **Calico was considered for the same treatment and deliberately left
fleet-wide** — `calico_version` is dual-owned with `gitops/` (the HelmRelease
chart version must match for clean adoption, and `crds/calico/crds.yaml` is
per-version), so per-cluster Calico forces a per-cluster gitops layout. Do that
when a second cluster exists, not before.

## 2026-08-03 — k3s v1.36.2 + Calico v3.32.1 applied; eBPF dataplane complete (both stages)

**Related:** [ADR-0019](decisions/0019-k3s-1.36-calico-3.32.1-version-pair.md) ·
[ADR-0024](decisions/0024-calico-ebpf-dataplane-no-kube-proxy.md) ·
[`calico-ebpf-single-node-trial.md`](calico-ebpf-single-node-trial.md) ·
commits `6312cf7`, `0f1ad01`, `f487044`

**Versions APPLIED: Calico `v3.32.1` + k3s `v1.36.2+k3s1`** (§7 items 15 + 16).
**They deliberately did NOT land with the encapsulation change**, contrary to
the original "one rebuild instead of two" plan: the BGP work was blocked on
pfSense values, while the version bump had no external dependency, so batching
them would have held a ready change hostage AND put two variables in one
rebuild. `values.yaml` therefore stayed `VXLANCrossSubnet` + `bgp: Disabled`
until 2026-08-16.

**The Calico eBPF migration is complete.** The cluster runs `linuxDataplane:
BPF` with **no kube-proxy at all**, all from committed config and verified on a
from-scratch rebuild. Source IP is preserved under `externalTrafficPolicy:
Cluster`, which **collapses the four-row matrix in the pfSense runbook §10 to
one row**.

**Result — `externalTrafficPolicy: Cluster` throughout, three requests each:**

| | Pod's observed `RemoteAddr` |
|---|---|
| Before (iptables/kube-proxy) | the **node's** address — SNAT |
| After (eBPF, Tunnel mode) | the **real off-cluster client** ✅ |

Environment: Flatcar `4593.2.4` / kernel `6.12.95-flatcar`, k3s `v1.36.2+k3s1`,
`tigera-operator-v3.32.1`, `bpfExternalServiceMode` left at default (Tunnel,
`DSR:false`). **Stage 1 re-verified on a from-scratch rebuild from committed
config**, so the result belongs to the repo rather than to hand-applied patches
— the hand-patches that set it up died with the previous node.

**Stage 2 removed kube-proxy entirely** (`disable-kube-proxy` in the k3s
config). ⚠ **The obvious verification is worthless here.** `kubectl -n
kube-system get ds kube-proxy` returning NotFound proves nothing on k3s — **k3s
embeds kube-proxy and never had a DaemonSet**. Combined with
[k3s#9561](https://github.com/k3s-io/k3s/issues/9561) (`disable-kube-proxy`
silently ignored from `config.yaml` while working as a CLI flag — fixed long
before 1.36, but silent when it bites), it is entirely possible to "verify"
stage 2, still be running kube-proxy, and still pass the source-IP test because
eBPF handles the packet first. What actually proved it, on the node:

| Check | Result |
|---|---|
| process named `*proxy*` in `/proc/*/comm` | none |
| `:10249` — kube-proxy's metrics port | not listening |
| residual `KUBE-SERVICES` iptables chains | **0** |
| `felix/kube-proxy.go` + `resync-kube-proxy-v4` in the reconcile loop | present — Calico's implementation is the live one |

⚠ **`:10256` IS listening, and that is correct.** It's kube-proxy's healthz
port, but the owner is Felix's `bpfKubeProxyHealthzPort` at its default —
Calico deliberately serves on the standard port so health checks expecting
kube-proxy keep working. This is the single most likely observation to make you
conclude, wrongly, that stage 2 failed.

Use `/proc/*/comm` rather than `ps | grep` — process *names* can't self-match,
whereas a grep pattern containing `kube-proxy` matches the shell running it.
(That produced a false positive first time through.)

`felixconfiguration.yaml` was deleted at stage 2, deliberately — it only
disabled `bpfKubeProxyIptablesCleanupEnabled` for coexistence; with kube-proxy
gone, leaving cleanup off would orphan its stale iptables rules. The 0-chain
count confirms Felix cleaned up. DSR remains NOT enabled.

Two things the bump dragged in, both resolved: v3.32 **removed the CRDs from
the chart** (new `gitops/crds/` tier, server-side applied — see the 2026-08-02
entry), and the PVE **snippet-dir permissions reset** on storage activation
(now self-repairing — below).

## 2026-08-03 — Ignition snippet destroyed after first boot (it holds the k3s join token)

**Related:** [ADR-0025](decisions/0025-destroy-ignition-snippet-after-first-boot.md) ·
`ansible/playbooks/tasks/destroy-ignition-snippet.yml` · commit `ee05242`

§7 item 21 raised and closed the same day. `provision-nodes.yml` now waits for
SSH on every node it provisioned, then detaches `cicustom` (API) and deletes
the `.ign` (SSH) — so the k3s join token no longer lives on shared snippet
storage past the one boot that reads it. Verified end-to-end on `snoop-a2o`
**including a full `qm reboot`**, which is the check that matters: the ordering
failure mode surfaces at *next start*, not at provision time.

- **Reboot evidence (`qm reboot 1050`, a full stop→start so PVE really
  regenerates the drive — a guest-side `reboot` would not test this):** `qm
  reboot` exits 0 (the ordering test itself — a stale `cicustom` dies exactly
  here), SSH answers in ~10 s, node **Ready**, all pods Running/Completed.
  Journal shows `ignition-subsequent.target — Subsequent (Not Ignition) boot
  complete` and `ignition-delete-config.service ... skipped
  (ConditionFirstBoot=true)` — Ignition provably did **not** re-run. `sr0` shows
  up labelled `cidata` but **mounted nowhere**.
- **Afterburn was the one real candidate, and it's clean.**
  `coreos-metadata-sshkeys@core.service` *does* run every boot and did rewrite
  `/home/core/.ssh/authorized_keys` — but its only source is
  `authorized_keys.d/ignition` (from the original Ignition run); no
  `coreos-metadata` source file appeared, matching the empty `sshkeys` in the
  dump. The `admin` user, whose keys we actually SSH with, is untouched.
- **Safe on a RUNNING VM — verified two ways in the PVE 9.2.3 source**, because
  "it lands in `[PENDING]`" would have made the ordering argument worthless:
  (1) every key of `$confdesc_cloudinit` — `cicustom` included — is in
  `$fast_plug_option`, so `vmconfig_hotplug_pending` applies the delete to the
  live config immediately; (2) even if deferred, `vm_start_nolock` applies
  pending changes and reloads the config before `apply_cloudinit_config`.
  Confirmed empirically: after the detach, `qm config --current 0` shows nothing
  pending.
- `ide2` deliberately stays (standard, comes back with every clone, and what PVE
  generates for it with `cicustom` gone carries nothing sensitive — `qm cloudinit
  dump 1050 user` shows no `sshkeys`, no `cipassword`).
- **Also landed:** the play asserts `node_filter` matched something (a typo used
  to provision nothing, silently — worse now that the same filter drives
  cleanup), and one node failing to boot no longer strands the *other* nodes'
  tokens.

## 2026-08-03 — Snippet-dir self-repair verified end-to-end — and its happy path found broken

**Related:** `ansible/roles/flatcar_vm/tasks/main.yml` · `ansible/README.md` →
"Proxmox SSH access" · commits `1a36365`, `0dd901a`

**§7 item 18 RESOLVED — the role now self-repairs.** `flatcar_vm` stats the
snippet dir, repairs group+setgid when it isn't writable, re-stats, then still
asserts. Repair needs root, so **two fixed-argument sudoers rules** were added
alongside `qm`, installed and verified on **all three PVE nodes**: positive
tests pass, and negative tests confirm the rules can't chmod another path or
chgrp another group. `pve-snippets` is **gid 1001 on all three** — worth having
checked, since the dir lives on shared CephFS which stores the numeric gid, so a
mismatched group id on one node would have failed only when Ansible happened to
target that node.

**The design premise changed, not just the config.** The README used to argue
`qm` was the *only* root command needed because the snippet write was handled
by owning the dir. Owning the dir turned out not to be durable, so that
rationale was rewritten rather than patched.

**✅ Repair path verified end-to-end** (third occurrence, this time
self-healed): `stat` → **repair `changed`** on both sudo commands → re-`stat` →
assert `ok` → upload `changed`, with no human involved. The resulting file is
`-rw-rw---- provisioner pve-snippets`, which simultaneously confirms the `0660`
join-token fix and that the setgid group inheritance works in practice.

**⚠ …but that verification covered only the REPAIR path, and the HAPPY path was
broken (found + fixed the same day, during item 21).** The re-stat reused
`register: snippet_dir`, and **a skipped task still registers** — as `{changed,
skipped, skip_reason}`, with no `stat` key. So whenever the dir needed *no*
repair, the skipped re-stat overwrote the good stat and the assert failed
`exists=n/a` against a perfectly healthy directory. **The polarity is what hid
it: it passed exactly when the dir was broken and failed exactly when it was
fine**, so the end-to-end check above — run on an occurrence — could only ever
see the passing case. Fixed by registering the re-stat as
`snippet_dir_repaired` and having the assert take `snippet_dir_repaired.stat |
default(snippet_dir.stat)`. Now verified on **both** paths. Lesson worth
keeping: *a fix verified only on the failure it was written for is
half-verified* — the branch where the problem is absent is a distinct path.

**The reset is cheaper to trigger than "a template rebuild" implies.** This
occurrence followed merely deleting the snippet. The storage root's mtime
changed a few minutes *before* the repair while `snippets/` changed at repair
time — and a parent's mtime only moves when an entry in it is created or
removed, so the content subdirectories were genuinely recreated. Assume **any**
storage-touching operation can do it; that's the case for self-repair over a
documented manual fix.

**Second fix, same class — security-relevant.** The upload was `mode: "0644"`,
and that `.ign` **embeds the k3s cluster join token**. It was world-readable,
protected only by the dir's `2770` — the exact state we just watched revert to
`0755`. Now `0660`, so exposure takes two independent regressions instead of
one.

## 2026-08-02 — Sequencing change: Calico BGP before Flux; the decisions batch

**Related:** [ADR-0018](decisions/0018-calico-bgp-replaces-metallb.md) ·
[ADR-0019](decisions/0019-k3s-1.36-calico-3.32.1-version-pair.md) ·
[ADR-0020](decisions/0020-crd-tier-vendored-server-side-apply.md) ·
[ADR-0021](decisions/0021-topology-blinding-postbuild-substitution.md) ·
[ADR-0022](decisions/0022-pfsense-frr-raw-config-explicit-neighbors.md) ·
[ADR-0023](decisions/0023-rfc8212-real-policy-le32.md) ·
[ADR-0024](decisions/0024-calico-ebpf-dataplane-no-kube-proxy.md) ·
commits `45625ef`, `2a413e5`, `9584ff1`, `3e55ffa`, `aaaa07c`

**The Calico BGP migration now comes BEFORE the Flux bootstrap.** Rationale: BGP
is not a late-tier LB feature here — it becomes the *dataplane* (§7 item 13
RESOLVED: Calico BGP owns both LB advertisement and pod routing, no MetalLB).
Calico's VXLAN implementation uses **no BGP at all**, so going no-encap takes
BGP from "not running" to "load-bearing for pod networking." Churning the CNI
was at its **cheapest right now** — one node, no workloads, nothing in `apps/`,
no PVCs, and Calico still *Ansible*-managed (helm revision 1), so re-priming is
an Ansible re-run rather than a fight with Flux. Every week that gets worse.

**Migration ordering — the risk window is node 2, not today.** With one node
the mesh has no peers to form, so flipping to no-encap is trivially safe. The
first moment mesh routing carries real traffic is when node 2 joins. So: flip
encapsulation now, establish and verify pfSense peering with only one node at
stake, and have **both proven before node 2 exists**. Note **changing an
existing IPPool's encapsulation is not a clean in-place edit** under the Tigera
operator; on an empty single-node cluster the honest path is to re-prime or
rebuild.

Decided the same day (each has its own ADR): Calico BGP replaces MetalLB for LB
advertisement, LB IPAM, and the pod dataplane; pin Calico `v3.32.1` with the
#12890 RBAC workaround pre-applied (an earlier draft recommended the `v3.31.x`
line — superseded); bump k3s to `v1.36.x` (we were on an EOL 1.32, two minors
below the oldest supported; re-provision, never sequential in-place upgrades;
don't chase 1.37, due 2026-08-26); the `gitops/crds/` tier; topology blinding
via `${var}` post-build substitution with the `StrictPostBuildSubstitutions`
gate; pfSense FRR managed as generated raw config; RFC 8212 satisfied by real
prefix lists rather than disabled; the eBPF single-node trial planned and
staged. The "secrets-ordering trap" (MetalLB's BGP password needed four steps
before ESO exists) was **resolved by stopping trying to move ESO earlier**:
anything needed before ESO is an Ansible-seeded Secret.

**⚠ The 3.32 CRD-removal gotcha — hit for real on the first bootstrap at the
new pin.** v3.32 **removed the CRDs from the tigera-operator chart** (its
`crds/` dir is empty; v3.29.1 shipped 5 files) — they moved to a separate
`crd.projectcalico.org.v1` chart. Symptom: the helm prime fails with *"no
matches for kind Installation / APIServer / Goldmane / Whisker in version
operator.tigera.io/v1 — ensure CRDs are installed first."* An
**install-contract change, not a bad pin**. **The obvious fix — a second
HelmRelease with `dependsOn` — does NOT work**: 3 of the 32 CRDs exceed the
262144-byte client-side apply limit (`installations` 1.46 MB, `gatewayapis`
466 KB, `istios` 284 KB), so they require server-side apply, which
helm-controller (and `helm install`) can't do. Resolved by the vendored
`gitops/crds/calico/crds.yaml` + a server-side-apply task in
`bootstrap-cluster.yml`. Also walked through v3.32's one-way gate
(AdminNetworkPolicy → ClusterNetworkPolicy) — verified a no-op, no policies
existed yet.

**eBPF preconditions verified live on `snoop-a2o`** (checked on the running
node, not inferred): kernel `6.12.95-flatcar` (needs ≥5.10), Flatcar 4593.2.4,
cgroup2 unified and writable, `/run` writable tmpfs, bpffs and debugfs already
mounted. ⚠ Do not apply Talos `cgroupV2Path` guidance to Flatcar — that is
[calico#7892](https://github.com/projectcalico/calico/issues/7892), Talos-specific.

**§7 item 19 raised — Flatcar OS update policy is unset by default.** There is
no `update-engine`/`locksmith` configuration anywhere in the Ignition
templates, so Flatcar's default auto-update-and-reboot is in force: nodes move
to new stable on their own schedule. Consequence for testing: eBPF behavior is
kernel-dependent, so **record OS + kernel with any trial result**. Related trap
— **the template is a pin by accident**: `flatcar_version: "current"` means a
rebuild fetches latest stable, but `flatcar_template`'s build is guarded on `qm
status <vmid>` failing, so `build-template.yml` **runs green and silently
skips** whenever vmid 9000 exists. See [ADR-0030](decisions/0030-flatcar-os-update-policy.md).

**§7 item 20 proposed — drop Helm for Calico and install the operator from
manifests.** Investigated while fixing the CRD split; `manifests/operator-crds.yaml`
is byte-for-byte identical to `helm template crd.projectcalico.org.v1` (40,019
lines each, `diff` clean). Deferred: the 1.36/3.32.1 rebuild was unverified and
landing this on top puts two variables in flight. See
[ADR-0029](decisions/0029-drop-helm-for-calico.md).

## 2026-08-02 — Snippet-dir permission trap found: it had silently blocked every worker node

**Related:** `ansible/README.md` → "Proxmox SSH access" · commit `cb08626`

Provisioning died on `Destination /mnt/pve/cephfs/snippets not writable`. Root
cause: the dir was `root:root 0755` instead of `drwxrws--- root pve-snippets` —
README step 4's `chgrp`/`chmod` half wasn't in effect, while the
`groupadd`/`usermod` half was (so `id` looked correct and the dir existed).

- **Why it hid for a month.** Ansible's `copy` checks the *directory* for
  writability **only when the destination file doesn't exist**; otherwise it
  checks the file. `snoop-a2o.ign` was already there from the §1 run, so the
  directory permission was never exercised. Deleting that file didn't create the
  bug — it removed the thing masking it.
- **⚠ It was a standing blocker for §6 step 2 (workers), not a one-off.** The
  destination is `{{ proxmox_snippet_dir }}/{{ hostname }}.ign` — **every new
  node writes a new filename**, so the first worker would have hit this
  regardless. §1's "verified end-to-end" never actually covered it.
- **⚠ It RECURS.** Observed 2026-08-02 and again 2026-08-03. PVE recreates
  storage content subdirectories as `root:root 0755` on **storage activation**,
  and *a template rebuild is enough to trigger it* (`qm destroy` + image import
  touch storage). The second occurrence was caught by the assert added after the
  first — which proved the mechanism but also proved that asserting only buys
  you a manual root `chgrp` every time it happens. The giveaway that it's this
  and not something else: the storage root and `snippets/` share an mtime.
  Self-repair followed on 2026-08-03 (above). The original `file:
  state=directory` check had passed happily against `root:root 0755` —
  *existence was never the thing in doubt.*

## 2026-08-01 — k3s sysext unattended patch update proven; Calico prime re-verified

**Related:** [ADR-0005](decisions/0005-flatcar-k3s-sysext-ignition-config-drive.md) ·
`ansible/roles/flatcar_vm/templates/k3s-sysupdate.conf.j2`

**§7 item 5 RESOLVED — k3s sysext + Flatcar works end-to-end, including
unattended patch updates.** All three legs proven on `snoop-a2o`:

- *Install + clean first boot:* proven at §2 on 2026-07-07 (after the
  initramfs-network fix).
- *Unattended patch update:* the node was **seeded at `v1.32.2+k3s1` and now
  runs `v1.32.3+k3s1`**, entirely on its own. `journalctl -u systemd-sysupdate`
  shows `systemd-sysupdate` selecting update `3+k3s1`, pulling
  `k3s-v1.32.3+k3s1-x86-64.raw` from `extensions.flatcar.org`, and installing
  it — 2026-07-12 18:14, ~3h after the 15:33 seed download. Both `.raw`s are
  retained in `/opt/extensions/k3s/` (under `InstancesMax=3`) and the
  `CurrentSymlink` `/etc/extensions/k3s.raw` was re-pointed to `.3`.
  `k3s --version` confirms the new binary took on the next boot.
- *Minor pin holds:* it moved within `v1.32` only, never across a minor — the
  whole point of the `k3s-<minor>` feature name / `MatchPattern`.
- **Cosmetic, worth a cleanup:** each run logs `Target specification lacks
  MatchPattern= expression. Assuming same value as in source specification.`
  Harmless (the assumption it makes is the one we want), but an explicit
  `MatchPattern=` in `[Target]` would silence it.
- **How to re-verify** after any change here: `ls -la /opt/extensions/k3s/`
  (instances) + `ls -la /etc/extensions/` (where `k3s.raw` points) + `sudo
  journalctl -u systemd-sysupdate | grep k3s`.

Practical consequence: `k3s_version` in `group_vars` is only the **seed** asset
for a fresh node, not what a long-running node runs. Expect a
provisioned-vs-running delta and don't treat it as drift.

**Calico prime re-verified:** node **Ready**, all
calico-system/calico-apiserver/tigera-operator pods Running, `tigera-operator`
helm release still at **revision 1** (primed once, never re-applied) and the
live `Installation` CR matching `values.yaml` exactly (`bgp: Disabled`, `cidr:
10.42.0.0/16`, `VXLANCrossSubnet`, `nodeAddressAutodetectionV4.kubernetes:
NodeInternalIP`) — the clean state Flux needs to adopt without a diff war.

## 2026-07-12 — Calico primed from Ansible; kubeconfig contexts; two questions raised

**Related:** [ADR-0016](decisions/0016-calico-ansible-primes-flux-adopts.md) ·
`ansible/playbooks/bootstrap-cluster.yml` · commit `b9deb9e`

**The Calico-prime half of §6 step 4 is done** in `playbooks/bootstrap-cluster.yml`
(wired into `site.yml` after `provision-nodes.yml`): it waits for SSH, polls the
k3s API `/readyz`, fetches `/etc/rancher/k3s/k3s.yaml` and rewrites it —
`server:` → the DMZ IP, and the entries renamed off k3s's `default` to the
cluster key from the node map (k3s names cluster+user+context ALL `default`, so
two clusters would silently clobber each other on any merge) — landing at
`ansible/.kube/<cluster>.config` (git-ignored) and *merged* (not overwritten)
into `~/.kube/config` via `kubernetes.core.kubeconfig`. Then `helm`-installs the
`tigera-operator` chart from `gitops/infrastructure/calico/values.yaml` and
waits for the node to go Ready. The play is per-cluster: it elects one bootstrap
primary per cluster in the node map.

**Flatcar gotcha (baked in): the node has NO Python**, so the tasks that run
*on* it use **`raw`** with `sudo` embedded — NOT `command`/`slurp`. The
Helm/`k8s_info` tasks avoid this entirely by running in a `hosts: localhost`
play against the cluster via kubeconfig.

**Raised — could Calico BGP absorb MetalLB?** Since we were already standing up
a BGP session to FRR for the LB range *and* Calico is the CNI, Calico's own BGP
could advertise LoadBalancer IPs directly — dropping MetalLB, and if the
dataplane also moves to BGP, removing VXLAN encapsulation (no ~50-byte encap
tax). Costs noted: couples pod networking to the pfSense BGP fabric, and leans
on Calico's newer LoadBalancer IPAM. **Decision at the time — crawl before
walk:** ship Calico VXLAN + `bgp: Disabled` with MetalLB first. *(Superseded
2026-08-02 — MetalLB was never written.)*

**Raised — §7 item 14, cluster-admin kubeconfig persists on the control node.**
*(Flux half de-scoped 2026-08-17 as a category error; hygiene half still open.)*

## 2026-07-08 — Calico bootstrap pattern decided: Ansible installs once, Flux adopts

**Related:** [ADR-0016](decisions/0016-calico-ansible-primes-flux-adopts.md)

The chicken-and-egg is real: Flux's own pods need a CNI, but Calico (the CNI)
is meant to be Flux-managed. Resolved by having **Ansible install Calico once**
during bootstrap, then letting **Flux adopt** the same release — *not* by baking
a k3s autoload manifest (`/var/lib/rancher/k3s/server/manifests/`), because
k3s's AddonManager continuously re-applies autoloaded manifests (Flux would
fight it) and deleting the manifest to "stop" autoload makes AddonManager
*prune* Calico. Mechanism: one pinned definition in `gitops/`, primed identically
by Ansible so Flux's first reconcile matches desired state.

## 2026-07-07 — k3s all-in-one server installed via the Flatcar k3s sysext (§2 done)

**Related:** [ADR-0005](decisions/0005-flatcar-k3s-sysext-ignition-config-drive.md) ·
`ansible/roles/flatcar_vm/` · commit `2d4dab8` (#2)

The `flatcar_vm` role bakes k3s into a node's Ignition when its node-map `role`
is a k3s role. A from-scratch rebuild comes up as a running k3s server with no
manual steps — same immutable-provisioning property §1 proved — though the
~50 MB sysext image is pulled once on **first boot**, so k3s is up ~30–60 s
after boot rather than instantly. Scope: server up, API serving, node
registered, secrets-encryption on, datastore on the data disk, auto-update
machinery wired. The node stays **NotReady** until Calico arrives — expected.

**What was added:** `k3s_version`/`k3s_minor` (seed asset, Renovate marker),
`k3s_cluster_cidr`/`k3s_service_cidr` (pinned), `k3s_token`/`k3s_tls_sans`
(vaulted at the time); `preflight.yml` derives `k3s_enabled`/`k3s_role`/`k3s_taint`
(all-in-one gets **no** CP taint); `templates/k3s-config.yaml.j2` and
`templates/k3s-sysupdate.conf.j2`, rendered to `files/` and pulled into
`butane.yaml.j2` via `contents.local:`; `butane.yaml.j2` gained its first `{% if
%}` block + `storage.links` and `systemd` sections.

**Findings baked into the implementation (were unknowns — design §7 item 5):**
- **Ignition can't fetch the sysext in this env — the image is downloaded on
  first boot instead.** The first attempt used `storage.files` with
  `contents.source:` (a remote fetch), which **boot-looped**: Ignition's files
  stage runs in the **initramfs**, which has **no network** here — no DHCP (hard
  guardrail) and the static `eth0` config only activates *after* the pivot. So
  the remote fetch hangs pre-pivot, Ignition never completes, and Flatcar
  reboots into it forever (symptom: console dead-ends in the initramfs disk
  stage, no shell, never reaches `Welcome to Flatcar`). Fix: a
  `k3s-sysext-download.service` oneshot (`After=network-online.target`) pulls
  the `.raw` from the **real root**, symlinks it into `/etc/extensions/`,
  re-merges systemd-sysext, then starts k3s. `Condition`-guarded on the `.raw`
  path so it runs only on first boot / rebuild. **Rule for this repo: never put
  a remote `contents.source:` in Ignition — the initramfs has no network.** We
  also do **not** let Ignition create `/etc/extensions/k3s.raw` (a dangling
  symlink at the early sysext merge could break the docker/containerd sysexts).
- **The k3s sysext ships NO auto-enable drop-in** (unlike `kubernetes.sysext`).
  The `k3s.service` unit lives *inside* the sysext, so it's absent at
  Ignition-provision time → enabled with a **`storage.links` wants/ symlink**,
  NOT `systemd.units[].enabled` (which would try to enable a not-yet-existing
  unit and fail).
- **sysupdate feature name embeds the minor** (`k3s-<minor>`): the transfer
  conf lives in `/etc/sysupdate.k3s-<minor>.d/`, `MatchPattern=k3s-<minor>.@v-%a.raw`,
  and the update is driven by `systemd-sysupdate -C k3s-<minor> update`. The
  name must match across all three. Mirrors the sysext-bakery.
- **Auto-update needs an explicit trigger**: the base `systemd-sysupdate.service`
  only updates the OS, so a drop-in runs the `-C k3s-<minor> update` as
  `ExecStartPre` and flags `/run/reboot-required` if the active `.raw` changed
  (the new binary applies on next boot). *(Pull-through proven 2026-08-01.)*
- **k3s datastore on the data disk — via the default path, NOT a `data-dir`
  override.** vdb is mounted at **`/var/lib/rancher`** (k3s's default data-dir
  root), so the kine/SQLite datastore + embedded containerd + image cache live
  on vdb while k3s runs stock. `k3s.service` gets an
  `After=systemd-sysext.service` + `RequiresMountsFor=/var/lib/rancher`
  drop-in. **Why not `data-dir: <custom>`:** the first cut did that
  (`/var/lib/data/rancher/k3s`) and `k3s secrets-encrypt` (+ `etcd-snapshot`,
  the uninstall script, community tooling) broke — they assume the default path
  and need `--data-dir` under an override. Mounting the disk *at* the default
  sidesteps all of it.

## 2026-07-07 — Flatcar VM shell provisioned end-to-end (§1 done)

**Related:** [ADR-0005](decisions/0005-flatcar-k3s-sysext-ignition-config-drive.md) ·
[ADR-0017](decisions/0017-static-addressing-no-dhcp.md) ·
[ADR-0007](decisions/0007-ansible-not-terraform.md) · commits `65199cb`, `c7f63d2` (#1)

Just the VM: powered-on Flatcar, correctly networked, persistent data disk
separate from the OS disk, SSH-reachable with key auth — surviving an unattended
`sudo reboot` and a from-scratch rebuild. **The full §1.4 definition of done
passed on `snoop-a2o`:**

- Static addresses from the node map on both `eth0` (DMZ) and `eth1` (Ceph
  public), each on its own subnet/VLAN — via `ip a`; no DHCP lease anywhere.
- Each interface's MAC matches what Ansible pinned via `proxmox_kvm`
  `net0`/`net1` — via `ip link show` — so `[Match] MACAddress=` is provably
  doing the binding rather than luck of interface-naming order.
- `eth1` negotiated MTU **8996**, not 1500.
- `eth1` has **no default route** — `ip route show dev eth1` shows only the
  connected Ceph public subnet.
- DNS resolves over `eth0` using the statically-configured resolvers.
- `hostname` matches the node map — via `hostnamectl`.
- `ssh <user>@<eth0-ip>` works with key auth, no password prompt possible.
- Data disk present, formatted, mounted — via `df -h` / `mount`.
- `sudo reboot` with no console attached comes back in the same state.
- Deleting the VM and re-running the play reproduces an identical result —
  same MAC, same static IP, same hostname (the real test of "rebuildable").

**This verification resolved §7 items 0–4 as originally posed:** (0) `eth1` on
the tagged Ceph-public VLAN comes up with the exact static address, no default
route, and MTU genuinely 8996; (1) the `storage.disks`/`storage.filesystems`
stanza formats and mounts the second virtio disk on first boot and survives a
delete-and-recreate; (2) MAC pinning via `proxmox_kvm` sticks, and
`load-node-map.yml` asserts hostname/`node_number` uniqueness in place of the
IP-collision protection DHCP used to give for free; (3) the proxmoxve image's
OEM consumes raw Ignition JSON from the `cicustom` config drive cleanly; (4)
`cicustom` is settable through the API path used by the role — no `qm`
fallback needed for it.

Implementation facts that came out of this step and still hold: addressing is
static, defined in Ignition's own `systemd-networkd` units (a different
mechanism from cloud-init's `network-data` path the design doc warned about);
`inventory/nodes.yml` is the source of truth, with everything host-shaped
derived from `node_number`; MACs are pinned at VM creation so `[Match]
MACAddress=` is deterministic regardless of guest interface naming; `eth1` gets
no `Gateway=`; DNS and hostname come from Ignition since DHCP is out of the
loop; `MTUBytes=8996` is set explicitly in both the Proxmox `net1` definition
and the networkd unit.
