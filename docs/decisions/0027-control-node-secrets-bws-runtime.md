# ADR-0027: Ansible reads Bitwarden Secrets Manager at run time; `vault.yml` is retired; secret zero is a macOS Keychain item

- **Date:** 2026-08-17 (decided and migrated) · 2026-08-30 (org id moved to the Keychain too)
- **Status:** Accepted — live
- **Supersedes / related:** supersedes the "secrets live in Ansible Vault" statements of the original design and the later "vault.yml is a BWS-materialized cache" position in the root `CLAUDE.md`; [ADR-0009](0009-secrets-aescbc-and-eso-bitwarden.md) (BWS for *app* secrets via ESO — the same store, a different consumer), [ADR-0021](0021-topology-blinding-postbuild-substitution.md) (why `cluster-topology` stays Ansible-seeded). Manifest of what exists in BWS: `ansible/BWS-SECRETS.md`. Code: `ansible/library/bws_secrets.py`, `ansible/playbooks/tasks/load-bws-secrets.yml`, `ansible/inventory/group_vars/all/vars.yml`.

## Context

Two contradicting statements had accumulated. The design doc said Ansible
Vault was the root of trust (the BW token "seeded from Ansible Vault"); the
root `CLAUDE.md` later said BWS was the root and `vault.yml` a materialized
cache of it. The arrow had reversed between them and nothing recorded it —
which is precisely how the question got asked again months later.

Underneath, `vault.yml` had the same properties that ruled out committed
ciphertext: no rotation, no revocation, no audit, one passphrase gating every
secret at once, and a long-lived encrypted blob of every credential sitting in
the working tree.

## Decision

**Bitwarden Secrets Manager is the store; there is no `vault.yml`.** Ansible
reads BWS at run time, in one bulk call at the top of every play. **Secret
zero is irreducible — it relocates, it does not disappear:** the BWS access
token can never itself come from BWS, so it lives in the **macOS Keychain**
(`security find-generic-password -w -s BWS_ACCESS_TOKEN -a $USER`, the
retrieval Bitwarden documents), read at task time. The organization id lives
there too (`BWS_ORG_ID`; env var or `-e` override still works — it's
environment-identifying, not a credential). Nothing secret then remains in
the repo directory at all.

- **Runtime lookup**: one source of truth, rotation takes effect on the next
  play run with no re-materialize step, every read is audited per-secret, and
  the machine token is scoped read-only to a single project with an expiry.
- **"But provisioning must work offline"** — a **non-scenario**. Provisioning
  is already internet-dependent at every step: the Flatcar image download, the
  k3s sysext from `extensions.flatcar.org` on first boot, the tigera-operator
  chart, every container image. BWS adds no new *class* of dependency. If the
  network is down you are fixing the network, not building a cluster.
- **Residual risk**: a Bitwarden-specific outage while the internet is
  otherwise up. Covered by the periodic encrypted SM export already in the
  break-glass plan ([ADR-0015](0015-backups-nas-s3-and-break-glass.md)) — that
  export is the offline copy, not a second live path.

### Layout: one secret per value, not grouped

~22 secrets in a single `homelab-infra` project, names mirroring the old
`vault_*` keys. The free tier caps *projects* (3), not secrets, so the count is
free. This preserves per-secret rotation and per-secret audit, and keeps
editing to pasting into a field. Two list-shaped secrets (`dns_servers`,
`ssh_authorized_keys`) are one entry per line.

### Fetch: a custom bulk module, not the stock lookup

`library/bws_secrets.py` wraps `bitwarden-sdk`: one or two API calls per run
regardless of secret count, returning a name→value dict. Wired in as
`tasks/load-bws-secrets.yml` at the top of each play — the pattern
`load-node-map.yml` already establishes — so `vars.yml` keeps its shape and
sources `{{ bws.<name> }}` instead of `{{ vault_<name> }}`.

**The limitation is the plugin's, not BWS's** — this is the bit worth
remembering. The SDK already exposes `list()`, `sync()` and `get_by_ids()`;
the collection simply doesn't surface them. So the fix belongs in ~50 lines of
our own code over Bitwarden's own SDK, not in contorting the secret layout to
fit a plugin's addressing model.

### Two BWS projects, split by CONSUMER, not by subject

- `homelab-infra` — read by the **control-node** machine account (token in the
  laptop Keychain, read-only). Proxmox credentials, network topology, SSH keys,
  k3s join tokens, the FRR password — **and ESO's own access token**.
- an **apps** project — read by the **ESO** machine account. Application
  secrets only. Create it at the ESO milestone; an empty project burns one of
  the free tier's three.

A project is the access-control unit (machine accounts are granted per
project), and the two consumers have very different exposure: the control-node
token sits in a laptop Keychain, while **ESO's token lives in a Kubernetes
Secret, readable by anything with cluster-admin or a pod exec in that
namespace**. Sharing a project means a cluster compromise hands over the
Proxmox API token, SSH keys and k3s join token — **cluster compromise
escalates to hypervisor compromise** via credentials ESO never needed. Two
projects make that step impossible rather than merely unlikely.

⚠ **`eso_bws_access_token` belongs in `homelab-infra`, not the apps project.**
It is read by the control node (which seeds it into the cluster); filing it
with the app secrets would let ESO read and rotate the credential gating its
own access. Same principle as secret zero: the thing that grants access cannot
live behind the access it grants.

## Alternatives rejected

- **Ansible Vault as the durable store** — no rotation, revocation or audit;
  one passphrase gates everything; and it is a second place credentials live
  that must be hand-reconciled with BWS forever. A local encrypted blob just
  moves the committed-ciphertext problems off Git rather than fixing them.
- **`vault.yml` as a BWS-materialized cache** (the recorded position, now
  overturned) — leaves **two** secrets to manage (the vault passphrase *and*
  the BWS token) and **two** sources that diverge silently: a stale cache is
  byte-indistinguishable from a fresh one until something breaks. It also
  preserves the long-lived encrypted blob of every credential in the working
  tree, which is the artifact BWS was adopted to remove.
- **Grouping several values into one secret as JSON** (considered to cut API
  calls) — **a BWS secret has no fields**: Name + Value + Notes, where Value
  is a single opaque string. "Grouping" means hand-authoring JSON into a plain
  textarea with no syntax awareness and no validation, where a missing comma
  surfaces as an Ansible failure much later. It also coarsens rotation (one
  credential rewrites the blob) and audit (the log names the group, not the
  field). And call count turned out to be the wrong lever anyway.
- **The stock `bitwarden.secrets.lookup` per variable** — takes **one secret
  UUID per call**, no name lookup, no list operation
  (`INVALID_SECRET_ID_ERROR`; the collection ships exactly one plugin). That
  means ~22 API calls **and** ~22 UUIDs replacing readable names in `vars.yml`
  — worse than what it replaces on both counts. ⚠ **The call count is not
  academic:** BWS enforces **undocumented** rate limits with no published
  threshold on any tier; forum reports describe throttling after six calls in
  succession, one specifically from "an Ansible playbook which looks up a dozen
  or so secrets in quick succession" — this exact workload. Design for one
  bulk fetch, not for a number.
- **Shelling out to `bws secret list`** — one call and readable names, but
  brittle (output parsing, binary on PATH, version drift) when the SDK is
  already a dependency.
- **One BWS project for everything** — the escalation path above.
- **A persisted SDK state file** — see Consequences; explicitly opted out.

## Consequences

- **⚠ Do NOT persist a state file — opt out explicitly.** The stock plugin
  defaults to `state_file_dir: ~/.config/bitwarden-sm-ansible`, and `bws` to
  `~/.config/bws/state` (one file per access-token id). Bitwarden describes
  these as "fully encrypted files that store authentication tokens and
  additional relevant data" whose purpose is to "reduce rate limiting *while
  authenticating*". **Neither the validity period nor the encrypting key is
  documented**; the token format suggests it's sealed with token-derived
  material — but that is inference, not verified — and "additional relevant
  data" may include the organization key, which is not short-lived. It is
  unnecessary here regardless: state files pay off across *many*
  authentications, and one bulk fetch per play authenticates once. Swapping
  `vault.yml` for an undocumented blob in `~/.config` is not the trade being
  made. `bws` opts out via `state_opt_out`; the custom module never writes one.
- **Cluster-bound does NOT mean ESO-managed.** `dmz_gateway`/`lb_range_base`
  reach the cluster as the Ansible-seeded `cluster-topology` Secret and must
  keep doing so **permanently** — ESO needs an LB IP that BGP produces
  ([ADR-0021](0021-topology-blinding-postbuild-substitution.md)).
- **Free-tier budget:** 3 projects and 3 machine accounts. Infra + apps = 2
  projects; control-node + ESO + the temporary migration-write account = 3, so
  **the write account had to be deleted after the port** or ESO has no slot.
  (Done; the one-shot port playbook was deleted with it.)
- **`bitwarden-sdk` is pinned deliberately** in `pyproject.toml` (open
  SDK-compatibility issue upstream, `bitwarden/sm-ansible#59`).
- **Keychain gotchas** (all verified on macOS 26.6.1):
  - **Not the Passwords app, and not Keychain Access.** Passwords.app manages
    the *synced iCloud Keychain* and only creates website/app logins and
    passkeys; `security` reads the *local* `login.keychain-db`. Items created
    by one are invisible to the other. Keychain Access.app was **removed in
    macOS 26** — Bitwarden's own docs still tell you to use it.
  - `security add-generic-password … -w` with **`-w` last** so it prompts
    (keeping the token out of shell history); mid-args it swallows the next
    flag as its value.
  - **Silent read, or prompt per read — your choice.** As created, `security`
    trusts itself so reads are silent. `-T ""` (empty trusted-application
    list) makes each read raise the keychain authorization dialog. ⚠ That
    dialog asks for the login password — **there is no Touch ID** (`security`
    has no biometry option; Touch ID would need the Secure-Enclave-backed
    data-protection keychain from a code-signed binary, buying ergonomics
    only). Cost: one prompt per **play**, so a full `site.yml` prompts four
    times. Click **Allow**, not *Always Allow* — the latter permanently defeats
    the point.
  - ⚠ **Org id ≠ project id.** Both are uuids, supplied a line apart. The
    wrong one fails as `404 Resource not found` on `sync()` *after* auth
    succeeds — that's the tell (a permissions problem would be `403`). Hit
    for real during the migration; the module now says so.
- The `ansible/.gitignore` patterns for `vault.yml*` stay as a backstop so a
  leftover or habit-created file can never be committed.
- **Things that will bite anyone repeating this:** the stock lookup is
  UUID-per-call with no name lookup; BWS rate limits are undocumented; a BWS
  secret has no fields; and `security` cannot see the Passwords app.

## Evidence

Migrated 2026-08-17 (`library/bws_secrets.py` bulk read, `load-bws-secrets.yml`
included once per play, `vars.yml` on `{{ bws.* }}`, `bitwarden-sdk` pinned).
Every play since — including the from-scratch `site.yml` run of 2026-08-30 —
has run with no `vault.yml` and no passphrase. See
[`../worklog.md`](../worklog.md).
