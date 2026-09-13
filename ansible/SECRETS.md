# Secrets — what to create in 1Password

The complete list of values this repo reads at run time. There is **no
`vault.yml`**; `playbooks/tasks/load-secrets.yml` fetches all of these into the
`secrets` fact, and `inventory/group_vars/all/vars.yml` indexes it. Nothing else
in the repo reads the store (ADR-0033).

Why 1Password, why the `op` CLI rather than the SDK, and what was rejected:
`docs/decisions/0034-secrets-store-1password.md`.

> **Blinding rule:** this file is committed. Formats below are illustrative and
> deliberately fake. Do not paste real values back into it.

### Scope, and why the filename is vendor-neutral when the contents are not

**This file covers the CONTROL-NODE half only** — what a human creates in the
store, and what Ansible reads out of it. It stops at the vault boundary.

**ESO's half is deliberately not here.** When that milestone lands, its
`ClusterSecretStore` names a provider, a vault and an auth Secret, and all three
are 1Password-specific; that configuration lives with the manifests it belongs
to, in `gitops/`, documented in `gitops/CLAUDE.md`. Only two things about ESO
appear below, and both are genuinely control-node concerns: **which vault** ESO
is granted (§1.3–1.4 — because a service account's grant is immutable, so it is
made once and cannot be widened), and **the item holding the token the control
node seeds** (§2).
Anything more would put a Flux-managed resource's schema in an Ansible doc,
where it would rot the first time the provider's CRD changed.

The **name** is what is vendor-neutral, and only the name. Contents that
describe a real store cannot be — you cannot document "create a service account
and grant it a vault" without naming whose service accounts those are. The point
of dropping `BWS-` from the filename is that the *next* store change is a rewrite
of this file's body rather than a rename plus a sweep of every link into it,
which is exactly the cost this migration is paying right now. Same reasoning as
naming the fact `secrets` rather than `bws` (ADR-0033).

---

## 1. One-time setup

### 1.1 Install the CLI

```sh
brew install 1password-cli      # macOS
op --version                    # must be >= 2.18.0
```

⚠ **>= 2.18.0 is a hard floor**, asserted by the load task. Below it `op`
ignores `OP_SERVICE_ACCOUNT_TOKEN` and falls back to an interactive session,
which inside a playbook simply hangs rather than failing.

`op` is an external binary prerequisite like `butane` and `helm` — see
`README.md`, "Control-node prerequisites". It is a static Go binary with no
libssl/glibc requirement, which is part of why it is used instead of the Python
SDK.

### 1.2 Authenticate — two modes, and the default needs no secret at all

`op` picks its mode from whatever credential is present. The load task supplies
a token only if one exists, so on a workstation the answer is "none".

| | Mode 1 — **desktop app** (default here) | Mode 2 — **service account** |
|---|---|---|
| Setup | 1Password app → Settings → Developer → **Integrate with 1Password CLI** | a token, from `-e`, `$OP_SERVICE_ACCOUNT_TOKEN`, or the Keychain (§1.5) |
| Unlock | Touch ID, then a session timeout | none — non-interactive |
| Scope | **everything you own**, read *and* write | read-only, named vaults only |
| Use it for | running plays by hand on your Mac | CI, a Linux control node, **ESO in-cluster** |

**Mode 1 is the default, and it is the interesting one: secret zero does not
relocate, it ceases to exist.** There is no token on disk, in a keychain, or in
the repo — nothing to leak, nothing to rotate, and nothing lost to 1Password's
"the token is shown exactly once" rule. It also sidesteps every Keychain
footgun in §1.5, and prompts *less* than the Keychain does (a session, versus
one authorization dialog per play).

```sh
# after enabling the integration in the app:
op vault list        # should list your vaults, unlocking with Touch ID
```

⚠ **The trade, stated plainly.** Mode 1 authenticates as *you*, so a playbook —
or anything that can invoke `op` while your session is unlocked — has read and
write access to every vault you own, where a service-account token is read-only
on its named vaults. What buys it back is that the operator at the keyboard
already has that access: the token was never a boundary against *them*, only
against a stolen laptop with an unlocked keychain, and Touch ID plus a session
timeout is the better answer to that threat than a keychain item that reads
silently forever. ADR-0032 originally argued for making mode 2 mandatory;
ADR-0034 overrules that **for the control node only**, and records why.

⚠ **It does not apply to ESO.** A pod has no desktop app, so ESO must use a
scoped service account regardless (§1.4). The consumer split therefore still
does the work it was designed for, on the side that actually needed it — a
compromised cluster still cannot reach the Proxmox token, nor another
cluster's app secrets.

⚠ **A locked app is indistinguishable from a missing one** from the CLI's side:
both surface as `No accounts configured for use with 1Password CLI`. Unlock it
before blaming the config.

### 1.3 Create the vaults

Split **by consumer, not by subject** — ADR-0027's split, kept — and then, on
the apps side, **one vault per cluster**:

| Vault | Read by | Holds |
|---|---|---|
| `homelab-infra` | the **control node** (one vault, fleet-wide) | everything in §2 — Proxmox credentials, topology, SSH keys, k3s join tokens, the FRR password — **and every cluster's ESO service-account token** |
| `<cluster>-apps` — e.g. `homelab-apps` | **that cluster's ESO** (token in a Kubernetes Secret) | application secrets for that cluster only. Created at the ESO milestone, one per cluster in `inventory/nodes.yml` |

```sh
op vault create homelab-infra
```

**Why the infra vault is fleet-wide but the apps vaults are per-cluster.** The
control node provisions every cluster, so a per-cluster split there would only
fragment one Proxmox credential across vaults that the same actor reads anyway —
no boundary gained. Per-cluster values already live in this vault as
**cluster-suffixed fields** (`k3s_token_<cluster>`), which is ADR-0026's
convention and needs no second vault.

ESO is the opposite case: each cluster runs its own, holding its own token in
its own Kubernetes Secret. ⚠ **This is the same argument that makes k3s join
tokens per-cluster (ADR-0026): a compromised cluster must not take the fleet
with it.** A `ClusterSecretStore` names exactly one vault and cannot reach a
second, so cluster A's ESO structurally cannot read cluster B's app secrets.

⚠ **This is a change in shape from ADR-0027, and the reason is worth knowing.**
That record specified *one* apps project — but BWS capped the free tier at **3
projects and 3 machine accounts**, which is also why its migration write account
had to be deleted to leave ESO a slot. A single shared apps project was what the
budget allowed, not what the blast-radius argument wanted. 1Password allows
**100 service accounts and unlimited vaults**, so the constraint is gone and the
per-cluster shape is simply affordable now.

⚠ **Every one must be a vault you created.** A service account can never read
a built-in Personal, Private or Employee vault, nor the default Shared vault.

The consumers have very different exposure: the control node authenticates as
you, at a keyboard, while ESO's token lives in a Kubernetes Secret readable by
anything with cluster-admin or a pod exec in that namespace. One vault for both
would mean a cluster compromise hands over the Proxmox API token, SSH keys and
k3s join token — **cluster compromise escalating to hypervisor compromise** via
credentials ESO never needed.

⚠ **`eso_op_service_account_token_<cluster>` belongs in `homelab-infra`, not in
any apps vault.** It is read by the control node, which seeds it into that
cluster. Filing it with the app secrets would let ESO read and rotate the
credential gating its own access — the same principle as secret zero: *the
thing that grants access cannot live behind the access it grants.* The
cluster suffix follows ADR-0026, exactly like `k3s_token_<cluster>`.

### 1.4 Service accounts — only where a desktop app cannot reach

**Skip this entirely if you run plays by hand on a Mac.** Mode 1 covers that,
and a service account you do not need is a credential you have to manage.

Create one for: **ESO** (mandatory — a pod has no desktop app), CI, or an
unattended/Linux control node.

```sh
# ESO — ONE PER CLUSTER, granted that cluster's apps vault ONLY.
# It must never reach homelab-infra, and never another cluster's vault.
op service-account create eso-homelab \
  --vault homelab-apps:read_items

# only if you also need unattended runs of these plays:
op service-account create homelab-control-node \
  --vault homelab-infra:read_items \
  --expires-in 180d
```

⚠ **One ESO service account per cluster, not one shared.** A shared token in
two clusters' Secrets means either cluster's compromise exposes both vaults,
which throws away the per-cluster split for nothing — the accounts are free
(100 of them) and the grant is the whole point.

⚠ **Service-account permissions and vault access are IMMUTABLE.** They cannot
be edited after creation; a different grant means a *new* service account and a
new token. Consequences worth reading twice:

- **Grant every vault it will ever need, at creation.** ⚠ For a control-node
  account, consider granting the apps vaults too: the still-open ESO-adoption
  question (`docs/decisions/README.md`) leans toward the control node seeding
  cluster-destined secrets it would have to *read* from an apps vault, and that
  grant cannot be added later. It widens no exposure — those secrets end up as
  in-cluster `Secret`s either way. An ESO account, by contrast, gets exactly
  one vault and never more.
- ⚠ **Defaulting to mode 1 removes this trap from the control-node side
  altogether**, which is a real argument for it: with no control-node service
  account, the only immutable grant in play is ESO's, and that one is
  unambiguous (`homelab-apps`, read-only).
- **`read_items` only.** Nothing in the run-time path writes. The one thing that
  does write — the migration in §4 — runs as *you*, not as any service account.
- **The token is displayed exactly once** and 1Password cannot show it again.
  Store it in the same sitting (§1.5). Losing it means a new service account,
  which means re-making the grant decision above.

Budget is not a constraint here: 100 service accounts and unlimited vaults,
against BWS's 3 projects / 3 machine accounts — the squeeze that forced deleting
the old migration write account is gone.

### 1.5 Storing a service-account token in the Keychain (mode 2 only)

**Not needed for mode 1.** This is for running these plays unattended on a Mac;
on CI or Linux, export `OP_SERVICE_ACCOUNT_TOKEN` instead and skip the rest.

```sh
security add-generic-password -a "$USER" -s OP_SERVICE_ACCOUNT_TOKEN -U -w
# -w MUST be last: with no value after it, security prompts (keeping the token
# out of shell history). Put -w mid-args and it swallows the next flag as its
# value instead of prompting.
security find-generic-password -w -s OP_SERVICE_ACCOUNT_TOKEN -a "$USER"   # verify
```

⚠ **Not the Passwords app, and not Keychain Access.** Those are different
stores: Passwords.app manages the *synced iCloud Keychain*, while the `security`
CLI reads the *local* `login.keychain-db`. Items created by one are invisible to
the other, and macOS nudging you toward Passwords is irrelevant here. (Keychain
Access.app was **removed in macOS 26**. Verified on 26.6.1: the `security` round
trip works.)

**Silent read, or prompt on every read — your choice.** As written above,
`security` trusts itself for items it created, so reads are silent. To require
authorization instead, create the item with an EMPTY trusted-application list:

```sh
security add-generic-password -a "$USER" -s OP_SERVICE_ACCOUNT_TOKEN -U -T "" -w
```

⚠ **That dialog asks for your login password — there is no Touch ID.** It is the
legacy SecurityAgent keychain-ACL prompt, and `security` has no biometry option
(its help lists only `-A` and `-T`).

⚠ **Cost: one prompt per PLAY, not per run.** The load task is included once per
play, so `render-frr-config.yml` prompts once, `bootstrap-cluster.yml` twice and
a full `site.yml` **four times**. In the dialog click **Allow**, not *Always
Allow* — the latter adds the caller to the ACL and permanently defeats the point.

**Want Touch ID?** That is mode 1 (§1.2) — delete this Keychain item and it is
what you get. If you specifically want Touch ID *and* a scoped read-only token,
store the token in a personal 1Password item and export it from your shell
profile; the precedence chain below already prefers the environment, so it needs
no repo change.

**Precedence**, highest first: `-e op_service_account_token=…` → the
`OP_SERVICE_ACCOUNT_TOKEN` environment variable (CI, Linux control nodes) → the
Keychain item.

⚠ **`OP_CONNECT_HOST` / `OP_CONNECT_TOKEN` beat all three.** 1Password gives
Connect precedence over service accounts, so a stray pair left over from another
project silently redirects every read. The load task refuses to run if they are
set, rather than authenticate as something you did not choose.

---

## 2. The item and its fields

**One item**, `control-node`, category **Secure Note**, in the `homelab-infra`
vault. Values are custom **fields**, grouped into purpose **sections**.

⚠ **Field labels ARE the secret names.** `group_vars/all/vars.yml` indexes
`secrets` by them, so they must match the first column exactly, and must be
**unique across every item** the account reads — not merely within one item.
1Password will happily let two items each carry a `base_domain`; the load task
refuses, because otherwise one silently wins and a value you never see is in
play.

⚠ The long opaque ones (`proxmox_api_token_secret`, `k3s_token_<cluster>`) fail
confusingly and late if truncated — paste, don't retype. Better still, don't
type any of them: §4 renders the whole item from the old store.

🔑 = a credential, and the field type must be **concealed**. The rest is
topology — still not for Git, but a different tier (root `CLAUDE.md`).

### Section: Proxmox

| Field label | Type | Format / example |
|---|---|---|
| `proxmox_api_host` | text | hostname or IP of the PVE API |
| `proxmox_api_user` | text | `ansible@pve` |
| `proxmox_api_token_id` | text | the token's ID part — ⚠ **not** a credential despite the name |
| **`proxmox_api_token_secret`** 🔑 | concealed | the token's secret part |
| `proxmox_node` | text | PVE node name, as PVE knows it |
| `proxmox_ssh_addr` | text | address the `pve` inventory alias connects to |
| `proxmox_ssh_user` | text | `provisioner` (or `root`) |
| `proxmox_vm_storage` | text | e.g. `local-lvm` |
| `proxmox_snippet_storage` | text | e.g. `cephfs` |
| `proxmox_snippet_dir` | text | e.g. `/mnt/pve/cephfs/snippets` |

### Section: Networks

| Field label | Type | Format / example |
|---|---|---|
| `dmz_subnet_base` | text | **first three octets only** — `10.0.1`, no trailing dot |
| `dmz_vlan` | text | integer as a string — `2` (cast with `\| int` on use) |
| `dmz_bridge` | text | `vmbr0` |
| `dmz_gateway` | text | full address — `10.0.1.1`. Also the BGP peer IP |
| `ceph_subnet_base` | text | first three octets only |
| `ceph_vlan` | text | integer as a string |
| `ceph_bridge` | text | `vmbr1` |
| `lb_range_base` | text | **first two octets only** — LB range is `<base>.<cluster index>.0/24` |
| `dns_servers` | text | **one per line** (see §3) |

### Section: Access

| Field label | Type | Format / example |
|---|---|---|
| `ssh_authorized_keys` | text | **one key per line** (see §3) |
| **`frr_master_password`** 🔑 | concealed | pfSense FRR daemon password |

### Section: Edge (cert-manager DNS-01 + the shared Gateway)

| Field label | Type | Format / example |
|---|---|---|
| `base_domain` | text | the apex zone, bare — `example.net`, no wildcard, no trailing dot |
| **`cloudflare_api_token`** 🔑 | concealed | Cloudflare API token with **Zone > DNS > Edit** *and* **Zone > Zone > Read** (cert-manager looks the zone id up by name), zone resources scoped to that one zone. ⚠ An **account-owned** token works but returns `Invalid API Token` from `/user/tokens/verify` — verify with `GET /zones` instead |

⚠ **These two are ZONE-scoped, not fleet-scoped** — they only look fleet-wide
because there is one zone today. The token is restricted to specific zone
resources, so a token for zone A cannot solve DNS-01 for zone B; if
`base_domain` ever forks, the token **must** fork with it. One scope, not two
(ADR-0035). Subdomains of a single zone (`<cluster>.example.net`) keep one
token serving everything.

`base_domain` reaches gitops/ as the `${base_domain}` placeholder via
`cluster-topology`; the token is seeded by `bootstrap-cluster.yml` into the
`cert-manager` namespace (bootstrap-secret tier — see the play's comments and
the ESO-adoption open question in `../docs/decisions/README.md`).

### Section: Storage (ceph-csi against the existing Proxmox Ceph)

| Field label | Type | Format / example |
|---|---|---|
| `ceph_fs_name` | text | PVE's **existing** CephFS name, from `ceph fs ls` — we join it, we don't create it |
| `ceph_mons` | text | Mon addresses, **one per line** (see §3) — `10.0.2.21:6789` |
| `ceph_fsid` | text | 36-char cluster uuid from `ceph fsid`; doubles as ceph-csi's `clusterID` |
| **`ceph_k8s_rbd_key`** 🔑 | concealed | cephx key for `client.k8s-rbd` (`ceph auth get-key`) |
| **`ceph_k8s_cephfs_key`** 🔑 | concealed | cephx key for `client.k8s-cephfs` |

**Only `ceph_fs_name` is needed up front** — the other four are *produced* by
`docs/proxmox-ceph-k8s-setup.md` §4.6, which prints them under exactly these
names. `render-ceph-setup.yml` checks for them softly and says which are
missing, so a first run is possible before they exist.

> ⚠ `ceph_mons` must be **public-network** addresses. The Ceph cluster
> (replication) network is untagged on the same bond as the public VLAN, so an
> address from it looks plausible and routes nowhere from a k3s node. Read them
> off `ceph mon dump`; the render asserts they're inside `ceph_subnet_base`.

The two keys are seeded by `bootstrap-cluster.yml` into the `ceph-csi` namespace
(bootstrap-secret tier — ceph-csi lands before ESO, so on a from-scratch rebuild
ESO cannot supply them); `ceph_mons`, `ceph_fsid` and the pool/group names reach
gitops/ as placeholders via `cluster-topology`.

### Section: Clusters — one pair per cluster in `inventory/nodes.yml`

Named `<prefix>_<cluster>`. With only `homelab` today that is one required field.

| Field label | Type | Required? |
|---|---|---|
| **`k3s_token_homelab`** 🔑 | concealed | **yes** for any cluster with k3s nodes |
| `k3s_tls_sans_homelab` | text | optional — omit the field entirely if no extra SANs |

Adding a cluster `edge` means adding `k3s_token_edge`. Nothing in the repo
changes; `vars.yml` assembles the map from the `clusters` keys.

⚠ **Omit an optional field rather than leaving it empty.** The load task drops
empty values so that absent and empty read the same, because `k3s_tls_sans_*` is
detected *by absence* — an empty string would satisfy a presence check while
contributing nothing. A misnamed one is caught loudly by the preflight assert
(ADR-0033); an empty one would not be.

### Later: the ESO service account

At the ESO milestone, a **second item** `eso-service-accounts` in this same
vault holds one concealed field **per cluster**,
`eso_op_service_account_token_<cluster>` 🔑 — the same cluster-suffixed naming
as `k3s_token_<cluster>` (ADR-0026), so `vars.yml` assembles the map from the
`clusters` keys exactly as it already does for tokens and SANs. Add
`eso-service-accounts` to `op_items` in `vars.yml` then; that costs one extra
API call per play.

Its own item, not fields on `control-node`, so the credentials gating cluster
access stay visibly and separately rotatable rather than having their history
coupled to everything else.

---

## 3. Multi-line values

Three fields hold lists: `dns_servers`, `ssh_authorized_keys`, `ceph_mons`.
**Put one entry per line** in the field — no commas, no JSON, no quoting. Blank
lines and stray whitespace are stripped.

```
ssh-ed25519 AAAA...  chris@laptop
ssh-ed25519 AAAA...  chris@desktop
```

1Password has no list field type either, so JSON here would reintroduce exactly
the hand-authoring problem that grouping avoided, for no gain.

⚠ **This is the one place the old BWS reasoning is INVERTED rather than
carried over.** Under Bitwarden, one-secret-per-value was forced: a BWS secret
has no fields — Name + Value + Notes, where Value is a single opaque string — so
grouping meant hand-authoring JSON into a textarea with no validation, where a
missing comma surfaced as an Ansible failure much later. **1Password items have
first-class fields with a label and a type, each independently editable in a
real form.** That premise is void, so grouping is now the *correct* shape, not a
compromise — and it is also what keeps the API call count at one per item
(ADR-0034).

---

## 4. Migrating from Bitwarden

Do not retype 29 values. `playbooks/render-1password-import.yml` reads the old
store and renders a 1Password item template:

```sh
cd ansible
uv run ansible-playbook playbooks/render-1password-import.yml
```

It prints how it classified every secret — name, section, and text-vs-concealed
— **before** anything is written, and refuses if any secret matched no section
rule. Review that list, then follow the commands it prints:

```sh
op signin                                     # as YOURSELF: this writes, and the
                                              # service account is read-only
op vault create homelab-infra
op item create --template .1password/control-node.json --vault homelab-infra --dry-run
op item create --template .1password/control-node.json --vault homelab-infra
rm -P .1password/control-node.json            # ⚠ do not skip
```

⚠ **The rendered file is every credential in plaintext**, 0600 in a 0700
git-ignored directory. It exists for the minutes between rendering and
`op item create`. A template is used rather than `op item create name=value …`
because 1Password's own CLI warns that assignment statements "get logged in your
command history, and can be visible to other processes on your machine."

⚠ **This play is one-shot.** Delete it once the cutover is done — the BWS
migration had a `port-vault-to-bws.yml` of the same shape and deleted it the
same day. It is the only thing in the repo that reads one store to populate
another, and that is not a capability worth keeping.

---

## 5. Verify

```sh
cd ansible
uv run ansible-playbook playbooks/render-frr-config.yml -v
```

`render-frr-config.yml` is the safest first run — it only reads inventory and
writes two local files, touching neither Proxmox nor any node.

At `-v` the load step prints **names and a count, never values**:

```
Loaded 29 secret(s) from 1Password vault 'homelab-infra' (1 item(s), 1 API call(s)): base_domain, ceph_bridge, ...
```

That list is the thing to read when a `{{ secrets.x }}` comes back undefined —
it tells you whether the field is missing from 1Password or just misspelled here.

### The cutover proof

Until the Bitwarden path is deleted, both backends are live and **must render
byte-identical output**:

```sh
uv run ansible-playbook playbooks/render-frr-config.yml
uv run ansible-playbook playbooks/render-frr-config.yml -e secrets_backend=bitwarden
```

Diff `.frr/` across the two runs. That diff is a far stronger check than reading
two lists of names side by side: it exercises every value through the same
templates, so a truncated paste or a swapped field shows up as a config
difference rather than as a silent success. Do the same with
`render-ceph-setup.yml`. **Only after both are clean** should the Bitwarden half
be removed (`docs/decisions/0034-secrets-store-1password.md` has the list).

### Rate limits

The daily cap is **per account, shared across every service account** — not per
token. On Individual/Families that is **1,000 requests / 24h** (Teams 5,000,
Business 50,000), plus 1,000 reads/hour per token.

This repo's Ansible usage is nowhere near it: one `op item get` per item per
play, so ~4 calls for a full `site.yml`. ⚠ **ESO is the one that would bite** —
roughly `N_externalsecrets × (24h / refreshInterval)`, about 480/day at the
default `1h` with 20 `ExternalSecret`s, and an invalid token can burn a *daily*
quota at retry cadence in minutes. Read ADR-0034's rate-limit section before
that milestone; it is a subscription-tier question, not an engineering one.

If the cap ever does bite here, pin `op_vault` and `op_items` to UUIDs rather
than titles — resolving a title costs an extra lookup per call, and passing ids
is 1Password's own advice for staying under the limit.

---

## 6. Common failures

| Symptom | Cause |
|---|---|
| `The 1Password CLI (op) is not on PATH` | `brew install 1password-cli` |
| `op X is too old` | Service accounts need >= 2.18.0. Below it, `op` ignores the token and hangs waiting for an interactive session |
| `No 1Password service account token` | Keychain item missing — or present but unreadable (locked keychain, dismissed authorization dialog). ⚠ Check with `security find-generic-password` before creating a second item; the lookup masks the difference so the missing-item case can report at all |
| `OP_CONNECT_HOST/OP_CONNECT_TOKEN are set` | Connect takes precedence over service accounts. `unset` them |
| `isn't a vault in this account` | Wrong `op_vault`, or no grant — ⚠ grants are immutable, so a missing one needs a new service account. `op vault list` shows what the token can see. Also: service accounts can never read built-in Personal/Private/Employee or default Shared vaults |
| `isn't an item` | Wrong title in `op_items`. `op item list --vault homelab-infra` |
| `(429) Too Many Requests` | Daily cap is per **account**, shared across service accounts, and does not reset on the hour. See §5 |
| `Duplicate field label(s)` | The same label exists on two items in scope; the load task refuses rather than picking a winner |
| `'secrets' is undefined` | A play that didn't include `tasks/load-secrets.yml` |
| `'dict object' has no attribute 'x'` | Field missing or mislabelled — compare against the `-v` list above |
| A secret silently absent | The field exists but its **value is empty**. Empty values are dropped deliberately, so that absent and empty read alike (see §2, Clusters) |
