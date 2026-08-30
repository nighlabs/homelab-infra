# Bitwarden Secrets Manager — what to create

The complete list of secrets this repo reads at run time. There is **no
`vault.yml`**; `playbooks/tasks/load-bws-secrets.yml` fetches all of these in a
single API call into the `bws` fact, and `inventory/group_vars/all/vars.yml`
indexes it.

Why it works this way — and why the values aren't grouped into JSON blobs:
`docs/mac-studio-inference-stack-2.md`, Appendix A, **"Control-node secrets"**.

> **Blinding rule:** this file is committed. Formats below are illustrative and
> deliberately fake. Do not paste real values back into it.

---

## 1. One-time setup

1. **Project** — create a Secrets Manager project, e.g. `homelab-infra`. The
   free tier allows 3 projects and unlimited secrets, so one project holds
   everything below.
2. **Machine account** — create one, grant it **read-only** access to that
   project only (never your personal vault), and **set an expiry**.
3. **Access token** — generate one for that machine account. ⚠ Bitwarden cannot
   show it again; it is never stored in their database.
4. **Keychain** — store the token:

   ```sh
   security add-generic-password -a "$USER" -s BWS_ACCESS_TOKEN -U -w
   # -w MUST be last: with no value after it, security prompts (keeping the token
   # out of shell history). Put -w mid-args and it swallows the next flag as its
   # value instead of prompting.
   security find-generic-password -w -s BWS_ACCESS_TOKEN -a "$USER"   # verify
   ```

   ⚠ **Not the Passwords app, and not Keychain Access.** Those are two different
   stores: Passwords.app manages the *synced iCloud Keychain*, while the
   `security` CLI reads the *local* `login.keychain-db`. Items created by one are
   invisible to the other. macOS nudging you toward Passwords is irrelevant here.
   (Keychain Access.app was **removed in macOS 26** — Bitwarden's own docs still
   tell you to use it. Verified on 26.6.1: the `security` round trip works.)

   **Silent read, or prompt on every read — your choice.** As written above,
   `security` trusts itself for items it created, so reads are silent. To require
   authorization instead, create the item with an EMPTY trusted-application list:

   ```sh
   security add-generic-password -a "$USER" -s BWS_ACCESS_TOKEN -U -T "" -w
   ```

   Nothing is pre-authorized, so each read raises the macOS keychain
   authorization dialog. Verified on 26.6.1: with `-T ""` the read blocks on the
   dialog; without it, it returns immediately.

   ⚠ **That dialog asks for your login password — there is no Touch ID.** It's
   the legacy SecurityAgent keychain-ACL prompt, and `security` has no biometry
   option (its help lists only `-A` and `-T`). Touch ID would need
   `SecAccessControlCreateWithFlags` on the Secure-Enclave-backed data-protection
   keychain, set programmatically and probably from a code-signed binary — which
   buys ergonomics only, since `-T ""` already gives the authorization-per-read
   property.

   ⚠ **Cost: one prompt per PLAY, not per run.** `load-bws-secrets.yml` is
   included once per play, so `render-frr-config.yml` or `provision-nodes.yml`
   prompt once, but `bootstrap-cluster.yml` prompts twice and a full `site.yml`
   **four times**. And in the dialog, click **Allow**, not *Always Allow* —
   the latter adds the caller to the ACL and permanently defeats the point.

   ⚠ **This is NOT the Passwords app**, and that app can't be used here. Two
   separate reasons: it manages the *synced iCloud Keychain* while `security`
   only searches the local file-based list (`security list-keychains`), and it
   only creates website/app logins and passkeys — there is no "arbitrary named
   secret" for `find-generic-password -s NAME` to match.
5. **Organization ID** — store it in the Keychain, the same way as the token
   (that is the default the plays read). Not a credential (it can't come from
   BWS, since you need it to make the call), but environment-identifying, so it
   isn't committed:
   ```sh
   security add-generic-password -a "$USER" -s BWS_ORG_ID -U -w   # -w last: prompts
   security find-generic-password -w -s BWS_ORG_ID -a "$USER"   # verify
   ```
   Prefer an env var instead (CI, Linux control nodes)? `export BWS_ORG_ID=…`
   takes precedence over the Keychain item; `-e bws_organization_id=…` beats
   both. Unlike the token, a *missing* org-id Keychain item is not an error — it
   just falls through to that env/`-e` path (the id isn't secret).

   ⚠ **This is the ORGANIZATION uuid, NOT the project uuid.** Both are uuids and
   you supply them one line apart during the import, which makes them easy to
   swap. Find the org id in the **web vault URL** while viewing the organization:
   `…/organizations/<this-uuid>/…`. The project uuid lives separately, inside
   Secrets Manager under Projects.

   Getting it wrong fails as **`404 Resource not found` on `sync()`** *after*
   authentication succeeds — which is the tell: auth working but the call
   404ing means the org id, not the token. A permissions problem would be `403`.
   (Hit for real during the migration; the module now says so in the error.)

   Override the Keychain/env defaults per-run with `-e bws_access_token=…` /
   `-e bws_organization_id=…` if you ever need to.

---

## 2. Secrets to create

> **Migrating from `vault.yml`? Don't hand-copy — use the helper.** Four values
> change shape between the two stores, and the long opaque ones
> (`proxmox_api_token_secret`, `k3s_token_<cluster>`) fail confusingly and late
> if truncated. `playbooks/port-vault-to-bws.yml` has two modes:
>
> **1. Dry run** (default) — reshapes everything into one file per secret,
> `0600` in a git-ignored `0700` dir. Prints names only, never values, and warns
> about any vault variable it didn't know how to port. Good as a preview even if
> you intend to import:
>
> ```sh
> uv run ansible-playbook playbooks/port-vault-to-bws.yml --ask-vault-pass
> ls .bws-port
> pbcopy < .bws-port/proxmox_api_token_secret   # paste by hand, if you prefer
> ```
>
> **2. Import** — creates every missing secret in BWS directly:
>
> ```sh
> uv run ansible-playbook playbooks/port-vault-to-bws.yml --ask-vault-pass \
>   -e bws_import=true \
>   -e bws_project_id=<project uuid> \
>   -e bws_write_token='<write-scoped token>'
> ```
>
> ⚠ **Import needs a WRITE-scoped token, which nothing else in this repo uses.**
> Create a **separate, temporary** machine account with write access on the
> project, import, then **delete that machine account**. Never widen the
> production read-only token — a read-only token that leaks can't write.
>
> The import is **idempotent and resumable**: it reads existing names once and
> creates only what's missing, skipping (never overwriting) anything already
> there. That matters because BWS rate limits are undocumented and there is no
> bulk create, so a 23-secret import can be throttled part-way — if it stops,
> re-run it. `-e bws_import_pause=2` spaces the calls out further.
>
> When done: `rm -rf .bws-port`, delete the temporary write machine account, and
> delete `playbooks/port-vault-to-bws.yml` + `playbooks/tasks/port-cluster-map.yml`.

**Every value is a plain string** — paste into the value field, no encoding.
Name them **exactly** as in the first column; the module keys the `bws` dict on
the secret name.

### Proxmox

| Secret name | Was | Format / example |
|---|---|---|
| `proxmox_api_host` | `vault_proxmox_api_host` | hostname or IP of the PVE API |
| `proxmox_api_user` | `vault_proxmox_api_user` | `ansible@pve` |
| `proxmox_api_token_id` | `vault_proxmox_api_token_id` | the token's ID part |
| **`proxmox_api_token_secret`** 🔑 | `vault_proxmox_api_token_secret` | the token's secret part |
| `proxmox_node` | `vault_proxmox_node` | PVE node name, as PVE knows it |
| `proxmox_ssh_addr` | `vault_proxmox_ssh_addr` | address the `pve` inventory alias connects to |
| `proxmox_ssh_user` | `vault_proxmox_ssh_user` | `provisioner` (or `root`) |
| `proxmox_vm_storage` | `vault_proxmox_vm_storage` | e.g. `local-lvm` |
| `proxmox_snippet_storage` | `vault_proxmox_snippet_storage` | e.g. `cephfs` |
| `proxmox_snippet_dir` | `vault_proxmox_snippet_dir` | e.g. `/mnt/pve/cephfs/snippets` |

### Networks

| Secret name | Was | Format / example |
|---|---|---|
| `dmz_subnet_base` | `vault_dmz_subnet_base` | **first three octets only** — `10.0.1`, no trailing dot |
| `dmz_vlan` | `vault_dmz_vlan` | integer as a string — `2` (cast with `\| int` on use) |
| `dmz_bridge` | `vault_dmz_bridge` | `vmbr0` |
| `dmz_gateway` | `vault_dmz_gateway` | full address — `10.0.1.1`. Also the BGP peer IP |
| `ceph_subnet_base` | `vault_ceph_subnet_base` | first three octets only |
| `ceph_vlan` | `vault_ceph_vlan` | integer as a string |
| `ceph_bridge` | `vault_ceph_bridge` | `vmbr1` |
| `lb_range_base` | `vault_lb_range_base` | **first two octets only** — LB range is `<base>.<cluster index>.0/24` |
| `dns_servers` | `vault_dns_servers` | **one per line** (see below) |

### Access

| Secret name | Was | Format / example |
|---|---|---|
| `ssh_authorized_keys` | `vault_ssh_authorized_keys` | **one key per line** (see below) |
| **`frr_master_password`** 🔑 | `vault_frr_master_password` | pfSense FRR daemon password |

### Per cluster — one secret per cluster in `inventory/nodes.yml`

Named `<prefix>_<cluster>`. With only `homelab` today that's one required secret.

| Secret name | Was | Required? |
|---|---|---|
| **`k3s_token_homelab`** 🔑 | `vault_k3s_tokens.homelab` | **yes** for any cluster with k3s nodes |
| `k3s_tls_sans_homelab` | `vault_k3s_tls_sans_by_cluster.homelab` | optional — omit entirely if no extra SANs |

Adding a cluster `edge` means adding `k3s_token_edge`. Nothing in the repo
changes; `vars.yml` assembles the map from the `clusters` keys.

🔑 = a credential. The rest is topology — still not for Git, but a different
tier (root `CLAUDE.md`).

---

## 3. Multi-line values

Two secrets hold lists. **Put one entry per line** in the value field — no
commas, no JSON, no quoting. Blank lines and stray whitespace are stripped.

```
ssh-ed25519 AAAA...  chris@laptop
ssh-ed25519 AAAA...  chris@desktop
```

That is the entire reason the layout is one-secret-per-value: a BWS secret has
no fields, so anything structured would mean hand-authoring JSON into a plain
textarea with no validation, where a missing comma surfaces as an Ansible
failure much later.

---

## 4. Verify

```sh
cd ansible
uv sync                                    # pulls bitwarden-sdk
export BWS_ORG_ID='<uuid>'
uv run ansible-playbook playbooks/render-frr-config.yml -v
```

`render-frr-config.yml` is the safest first run — it only reads inventory and
writes two local files, touching neither Proxmox nor any node.

At `-v` the load step prints **names and a count, never values**:

```
Loaded 22 secret(s) from BWS: ceph_bridge, ceph_subnet_base, ...
```

That list is the thing to read when a `{{ bws.x }}` comes back undefined — it
tells you whether the secret is missing from BWS or just misspelled here.

**Common failures**

| Symptom | Cause |
|---|---|
| `No BWS access token` | Keychain item missing or named differently; check the `-s`/`-a` values in step 1.4 |
| `Access token is not in a valid format` | Truncated paste, or a Password Manager token rather than a Secrets Manager machine-account token |
| `BWS sync() failed … 404 Resource not found` | Wrong `bws_organization_id` — most often the **project** uuid pasted in its place. Auth succeeded, so it isn't the token. |
| `BWS sync() failed … 403` | The machine account has no grant on the project (different from a 404) |
| `'bws' is undefined` | A play that didn't include `tasks/load-bws-secrets.yml` |
| `'dict object' has no attribute 'x'` | Secret missing or misnamed — compare against the `-v` list above |
| `Duplicate secret name(s)` | The same name exists twice in scope; the module refuses rather than picking a winner |
