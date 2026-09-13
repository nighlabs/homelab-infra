# ADR-0034: 1Password replaces Bitwarden Secrets Manager; Ansible reads it with the `op` CLI; the control node keeps no secret zero

- **Date:** 2026-09-12 (decided and implemented; cutover verification outstanding)
- **Status:** Accepted — implemented, parallel-run verification pending
- **Supersedes / related:** **supersedes [ADR-0027](0027-control-node-secrets-bws-runtime.md)** (control-node secrets from BWS at run time) and **answers [ADR-0032](0032-secrets-store-1password-migration.md)** (which posed this as an open question and did the investigation this record acts on); **partially supersedes [ADR-0009](0009-secrets-aescbc-and-eso-bitwarden.md)** — its *layer 2* (ESO + Bitwarden) only; **layer 1 (k3s aescbc at rest) is untouched and live**. Related: [ADR-0033](0033-secrets-fact-broker.md) (the broker seal that made this a two-file change), [ADR-0021](0021-topology-blinding-postbuild-substitution.md) (`cluster-topology` stays Ansible-seeded, on grounds corrected below), [ADR-0015](0015-backups-nas-s3-and-break-glass.md) (break-glass export mechanism changes), [ADR-0013](0013-ingress-certs-dns-external-access.md) (DNS-01, which is why the old ESO ordering chain was already overstated). Code: `ansible/playbooks/tasks/load-secrets.yml` + `load-secrets-onepassword.yml`, `ansible/playbooks/render-1password-import.yml`, `ansible/inventory/group_vars/all/vars.yml`, `ansible/SECRETS.md`.

## Context

[ADR-0032](0032-secrets-store-1password-migration.md) asked whether to move, did
the investigation, and deliberately stopped short of deciding. Its central
finding was that the answer gets more expensive after the ESO milestone than
before: the cluster half is **greenfield**, not a migration — no ESO manifests
exist, only reserved slots — and on that side 1Password is *strictly less work*
than Bitwarden would have been, because the SDK provider runs **no in-cluster
server**, so there is no Deployment, Service, `Certificate` or image digest to
maintain. Porting ESO twice was the expensive path.

This record decides it: **move, control node first.**

Every vendor claim ADR-0032 rested on was re-checked against current upstream
documentation on 2026-09-12 before acting, as that record asked. All held:
service accounts exist on Individual/Families; the daily cap is per *account*,
shared across service accounts; permissions and vault access are immutable;
`--vault` is mandatory when a credential can see more than one vault.

## Decision

**1Password is the durable store.** The control node reads it at run time with
the **`op` CLI**; `inventory/group_vars/all/vars.yml` indexes the result exactly
as before, because field labels are the secret names and ADR-0033 had already
reduced the store's blast radius to two files.

### The `op` CLI, not the Python SDK — reversing ADR-0027 on its own terms

ADR-0027 rejected "shelling out to `bws secret list`" as *"brittle (output
parsing, binary on PATH, version drift) when the SDK is already a dependency."*
Every clause of that was Bitwarden-shaped and none survives the move:

- **"when the SDK is already a dependency"** — it is not. 1Password's Python SDK
  would be a **new** dependency, **0.x with documented breaking changes between
  minors** (worse than the 1.x it replaces), carrying a **native libssl 3 /
  glibc 2.32+** requirement that constrains a Linux control node or CI runner.
  `op` is a static Go binary with no such constraint.
- **"output parsing"** meant scraping a human-readable table. `op item get
  --format json` is a published contract — the same shape `op item create`
  consumes.
- **"binary on PATH"** is real, and is the price paid. This repo already depends
  on `butane`, `helm` and `kubectl` exactly this way; `op` joins that list.

The SDK's one genuine advantage — richer "no vault by that title; this account
can see …" diagnostics — is bought back by hand, in the load task's assert.

**Net: the store change costs us a *binary* prerequisite and removes a Python
one, a custom Ansible module, and an async-wrapper's worth of code.**

### Grouped fields, not one secret per value — reversing ADR-0027's other premise

ADR-0027 rejected grouping on one explicit premise: *"a BWS secret has no
fields"* — Name + Value + Notes, where Value is one opaque string, so grouping
meant hand-authoring JSON into a textarea with no validation. **1Password items
have first-class fields with a label and a type, each independently editable in
a real form.** The premise is void, so grouping is now the *correct* shape
rather than a JSON hack — and it is what keeps the call count at one per item.

One `control-node` item, category Secure Note, fields grouped into purpose
sections. Multi-line values keep the one-per-line convention: 1Password has no
list type either, so JSON would reintroduce hand-authoring for no gain.

⚠ Field labels must be globally unique across every item the credential reads —
**1Password does not enforce this**, so the load task refuses on a duplicate.
ESO's own provider has the same constraint ("found multiple labels with the same
key"), so a layout that trips it here would have tripped it there.

### ⚠ The control node keeps NO secret zero — a reversal of ADR-0032's own recommendation

ADR-0027's framing was *"secret zero is irreducible — it relocates, it does not
disappear"*, and ADR-0032 carried that forward, explicitly warning **not** to
make `op`'s biometric unlock the default because it *"authenticates as you, with
your full vault access, discarding the least-privilege property the consumer
split exists to protect, and it is interactive-only."*

**That is overruled for the control node, and the trade is recorded rather than
glossed.** `op` authenticates against the local 1Password desktop app, so on a
workstation **there is no token at all** — not on disk, not in a keychain, not
in the repo. Secret zero does not relocate here; it *ceases to exist*.

What is genuinely given up: the desktop app authenticates as the operator, with
read *and* write on every vault they own, where a service-account token is
read-only on two named vaults.

Why that is acceptable **here specifically**:

- **The operator at the keyboard already holds that access.** The token was
  never a boundary against *them*; it was a boundary against a stolen laptop
  with a silently-readable keychain item. Touch ID plus a session timeout is a
  strictly better answer to that threat than a Keychain item that, as
  configured, reads without any prompt at all.
- **It removes the immutable-grant trap from this side entirely.** Service
  account vault grants cannot be edited after creation, and ADR-0032 flagged
  that the still-open ESO-adoption question forces a "grant both vaults now"
  guess. With no control-node service account, the only immutable grant left is
  ESO's, and that one is unambiguous.
- **It deletes a whole class of documented footguns** — `-w` must be last,
  `-T ""`, Passwords.app vs `login.keychain-db`, Keychain Access removed in
  macOS 26, "the token is shown exactly once".

⚠ **It is overruled for the control node ONLY.** ESO has no desktop app and
keeps a scoped service account, as does CI or a Linux control node. The
consumer split therefore still does the work it was designed for, on the side
that actually needed it: **a compromised cluster still cannot reach the Proxmox
token.** The load task supports both — a token is used when present, and the
desktop app when not.

### Vault layout: ADR-0027's split by CONSUMER, preserved and strengthened

`homelab-infra` (control node) and `homelab-apps` (ESO). ⚠ `eso_op_service_account_token`
stays in `homelab-infra` — *the thing that grants access cannot live behind the
access it grants.*

Under 1Password this becomes **structural rather than a matter of discipline**: a
SecretStore names exactly one vault and cannot reach a second, so "cluster
compromise must not escalate to hypervisor compromise" is closed by the API
shape. Budget stops being a constraint too: 100 service accounts and unlimited
vaults, against BWS's 3 projects / 3 machine accounts.

### Migration by rendered template, not by retyping

`playbooks/render-1password-import.yml` reads the old store and renders a
1Password item JSON template — the same "generate the content, deliver it by
hand" shape as `render-ceph-setup.yml` and `render-frr-config.yml`, and for the
same reason: the target has no safe automation path. Two specifics:

- **A template file, not assignment statements.** 1Password's own CLI warns that
  `name=value` arguments "get logged in your command history, and can be visible
  to other processes on your machine." The rendered file keeps values out of
  argv entirely.
- **It renders rather than writes** because creating the item needs *write*
  access, which no run-time credential here has. The human already has it.

⚠ The rendered file is every credential in plaintext — 0600 in a 0700
git-ignored directory, and the play's last printed instruction is to shred it.
⚠ The play is **one-shot** and is deleted after cutover, exactly as ADR-0027's
`port-vault-to-bws.yml` was.

### Both backends live during cutover

`tasks/load-secrets.yml` is a dispatcher over `secrets_backend`
(`onepassword` | `bitwarden`). Rollback is one `-e`, not a revert of deleted
code, and the **byte-identical render diff across backends is the cutover
proof** — far stronger than comparing two lists of names, because it pushes
every value through the real templates.

## ⚠ The rate limit: the one axis where this is a regression

| Plan | Requests / 24 h (per **account**) | Reads / hour (per token) |
|---|---|---|
| Individual & Families | **1,000** | 1,000 |
| Teams | 5,000 | 1,000 |
| Business | 50,000 | 10,000 |

The daily cap is **per account, shared across every service account** — not per
token — and does not reset hourly. Bitwarden's limits were *undocumented*
(ADR-0027 cites throttling after six calls in succession); a published limit you
can design against is better than an unknown one you discover by tripping it,
but **1,000/day is a real ceiling BWS never imposed in practice.**

- **Ansible fits comfortably**: one `op item get` per item per play, ~4 calls
  for a full `site.yml`.
- ⚠ **ESO is where it bites.** Steady state is roughly
  `N_externalsecrets × (24h / refreshInterval)` — ~480/day at the default `1h`
  with 20 `ExternalSecret`s, about half an Individual account's allowance before
  the Postgres → Redis → LiteLLM stack exists.
- ⚠ **Error amplification is the real hazard, not steady state.**
  [external-secrets#4925](https://github.com/external-secrets/external-secrets/issues/4925)
  records an invalid token consuming quota at retry cadence and exhausting a
  **daily** limit in ~20 minutes. On Business that is noise. On Individual it is
  a 24-hour lockout of ESO *and* any service-account path — ADR-0027's
  "provisioning must work offline is a non-scenario" argument covers a network
  outage, not self-inflicted quota exhaustion.

**Accepted knowingly on Individual/Families**, on the basis that the control
node no longer consumes the quota at all in normal operation (the desktop app is
not a service account) and that the ESO milestone must set `refreshInterval`
deliberately. ⚠ **Re-evaluate the plan tier at that milestone** — it is a
subscription question, not an engineering one.

## Alternatives rejected

- **Stay on BWS.** Costs nothing today, and the control-node half was live and
  verified. Rejected because the ESO milestone would then build and maintain an
  in-cluster SDK Server with a cert-manager certificate, and because the cheap
  version of this decision expires the moment ESO ships on Bitwarden.
- **1Password for ESO only, BWS retained on the control node.** Takes the
  SDK-Server win without touching working Ansible, but costs two stores, two
  secret zeros, two manifests to reconcile, and no single store to split by
  consumer. ADR-0027's Context is *literally* about the cost of two
  contradicting sources of truth.
- **The 1Password Python SDK** — see above; a new 0.x native-dependency for
  strictly less than the CLI gives.
- **`op read op://…` per value** — one call per field, which is
  `bitwarden.secrets.lookup` wearing a new hat, and far worse under a hard
  daily cap.
- **1Password Connect** — carries a deprecation notice pointing at the
  SDK/service-account path.
- **Making the desktop app the default for ESO too** — impossible; a pod has no
  desktop app. This is why the reversal is scoped to the control node.
- **Pinning vault/item UUIDs by default** — fewer API calls, but a uuid in
  `group_vars` is unreadable and this repo's call volume is two orders of
  magnitude below the cap. Documented as the lever to pull if it ever bites.

## Consequences

- **ADR-0027 is superseded.** ADR-0009 is **partially superseded — layer 2
  only**; layer 1 (k3s aescbc at rest) is unchanged and live. ⚠ That status form
  did not exist in the index before this record and is introduced here.
- **The ESO ordering chain is corrected, not reworded.** "ESO sits below
  cert-manager" is now **false**: no in-cluster server, no certificate. ESO's
  earliest position is immediately after Calico. The reference docs say where
  the old rule came from so it is not reintroduced from memory. (ADR-0032 had
  already shown the chain was *partly* overstated even under Bitwarden, since
  DNS-01 needs no Gateway and no LB IP.)
- **Two open questions are now coupled** and must be decided together at the ESO
  milestone: whether ESO *adopts* the bootstrap-seeded Secrets, and whether
  `infrastructure-config` splits per domain. The latter's only hard forcing edge
  was the SecretStore's dependence on a cert-manager `Certificate`, which is
  gone; the one way to reintroduce one is to source cert-manager's Cloudflare
  token *from* ESO.
- **`ansible/BWS-SECRETS.md` → `ansible/SECRETS.md`.** ⚠ The *name* is
  vendor-neutral; the contents cannot be, and are not pretending to be. The
  point is that the next store change is a rewrite of one file's body rather
  than a rename plus a sweep of every link into it. It covers the control-node
  half only and **stops at the vault boundary** — ESO's provider config lives
  with its manifests in `gitops/`, where a CRD schema change will not rot an
  Ansible doc.
- **`bitwarden-sdk` stays pinned in `pyproject.toml` for now**, with
  `library/bws_secret*.py` and the Bitwarden backend task. All four are deleted
  together after the parallel run.
- **ADR-0015's break-glass argument survives; the mechanism changes** — the
  offline copy becomes a periodic encrypted 1Password export plus the account's
  Emergency Kit and recovery code.
- ⚠ **Per-access audit is a paid-tier reporting feature.** Worth noting that
  ADR-0009's symmetrical "logs every access" claim about Bitwarden may not have
  been accurate on the free tier either.
- **A latent ADR-0027 bug was found and fixed on the way.** The Keychain `pipe`
  lookup turns `security`'s exit-44 into a hard error *while resolving the
  variable*, before the assert that explains how to create the item — so that
  carefully-written message could never render. The 1Password path masks the
  exit and names the locked/denied case that masking hides. ⚠ The retained BWS
  command still has the old behaviour and is not being fixed, since it is
  scheduled for deletion.

### Removal criteria for the Bitwarden half

Delete **together**, and only after `render-frr-config.yml` *and*
`render-ceph-setup.yml` produce byte-identical output under both backends:

1. `ansible/playbooks/tasks/load-secrets-bitwarden.yml`
2. `ansible/library/bws_secrets.py` and `ansible/library/bws_secret.py`
   (and then `library = ./library` in `ansible.cfg`, which empties out)
3. the `bitwarden-sdk` pin in `pyproject.toml`
4. the `bws_*` connection variables in `group_vars/all/vars.yml`, and
   `secrets_backend` with them
5. `ansible/playbooks/render-1password-import.yml` (one-shot)
6. the `BWS_ACCESS_TOKEN` / `BWS_ORG_ID` Keychain items, and the BWS machine
   account itself

⚠ Leaving both stores live indefinitely recreates precisely the "two
contradicting sources of truth" that ADR-0027 was written to repair.

## Evidence

Implemented 2026-09-12. Verified against the live BWS store (29 secrets) and a
`.frr`/`.ceph` baseline captured before any change:

- **The import renderer produces valid 1Password item JSON**: all 29 secrets
  classified into 6 purpose sections with **nothing** in the catch-all (which is
  an assert, not an observation), labels unique, every value non-empty, correct
  top-level keys, and both `STRING` and `CONCEALED` types present. ⚠ The
  concealed set is an explicit list, not a pattern: `proxmox_api_token_id` is
  **not** concealed while `proxmox_api_token_secret` is, and a `_token_` rule
  would have got that backwards while looking right.
- **The Bitwarden backend still renders byte-identical output** through the new
  dispatcher, re-checked after every subsequent commit — this is the control for
  the whole migration.
- **The 1Password path reaches `op` correctly and reports the right mode.** With
  no account configured it identifies desktop-app mode and returns the
  enable-the-integration instruction; `op` 2.39.0 satisfies the >= 2.18.0
  assert.
- All seven playbooks pass `--syntax-check`.

⚠ **Not yet verified, and the thing that actually closes this record:** no
1Password vault or item exists yet, so the **byte-identical parallel run across
both backends has not been performed.** Until it has, the Bitwarden half must
not be deleted and this ADR stays "implemented, verification pending".
