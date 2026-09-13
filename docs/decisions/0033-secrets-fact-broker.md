# ADR-0033: `vars.yml` is the only broker for secret *values*; name-space questions get their own `secret_names` fact

- **Date:** 2026-09-12 (raised, implemented and verified the same day)
- **Status:** Accepted, verified
- **Supersedes / related:** [ADR-0027](0027-control-node-secrets-bws-runtime.md) (established the `bws` fact and the one-bulk-fetch shape; this refines how it is consumed, and does not reverse it), [ADR-0032](0032-secrets-store-1password-migration.md) (a possible store change — this is worth doing whether or not that happens, and makes it much cheaper), [ADR-0026](0026-per-cluster-derivation-from-index.md) (the per-cluster `k3s_token_*` / `k3s_tls_sans_*` secrets whose *names* are the thing enumerated). Code: `ansible/inventory/group_vars/all/vars.yml`, `ansible/playbooks/tasks/load-bws-secrets.yml`, `ansible/inventory/hosts.yml`, `ansible/roles/flatcar_vm/tasks/`, `ansible/playbooks/render-ceph-setup.yml`.

## Context

ADR-0027 loads every secret in one call into a single `bws` fact, and
`inventory/group_vars/all/vars.yml` indexes it into named variables
(`proxmox_api_host`, `ceph_csi.mons`, `k3s_tokens`, …). The intent is that
`vars.yml` is the **single indirection layer**: a play or role consumes
`{{ proxmox_api_host }}`, never `{{ bws.proxmox_api_host }}`, so the store is
reachable from exactly one file.

That intent is mostly honoured — `vars.yml` holds ~22 of the references — but
it was never enforced, and `bws.*` has escaped into four other files. Verified
by grep on 2026-09-12:

| Site | Refs | Kind |
|---|---|---|
| `inventory/hosts.yml` | 2 | plain leak |
| `roles/flatcar_vm/tasks/main.yml` | 2 | plain leak |
| `roles/flatcar_vm/tasks/preflight.yml` | 1 | **structural** |
| `playbooks/render-ceph-setup.yml` | 5 | **structural** |

The distinction matters, and it is the whole reason this is a record rather
than a chore.

**The two plain leaks are ordinary omissions.** `hosts.yml` reaches through
`hostvars['localhost'].bws.proxmox_ssh_addr` / `.proxmox_ssh_user` because
those two values have **no brokered variable at all** — this is their only
consumer, so nobody noticed. `flatcar_vm/tasks/main.yml` interpolates
`bws.proxmox_ssh_user` into a snippet-directory remediation message.

**The two structural leaks are not omissions — the broker cannot express what
they need.** They do not ask for a value; they ask a question about the **set
of secret names**:

- `preflight.yml` asserts that every `k3s_tls_sans_*` secret names a cluster
  that exists in `nodes.yml`, via `bws | select('match', '^k3s_tls_sans_')`.
  (Iterating a dict in Jinja yields its keys, which works but is subtle enough
  to be worth naming.) That guard exists because the secret is optional, so a
  typo would otherwise **fail silently** — the SANs simply never reach any
  node's config, surfacing much later as a TLS error.
- `render-ceph-setup.yml` reports which of the five cluster-side secrets exist
  yet, via `expected | reject('in', bws.keys() | list)`, and gates two asserts
  on `bws.ceph_mons is defined` / `bws.ceph_fsid is defined`. These are
  deliberately **not** asserts: on a first run those secrets cannot exist,
  because the rendered script is what produces them.

A brokered variable is a value. Neither of these wants a value, so both had to
reach past the broker to get at the key namespace. **There is no way to write
them correctly today** — which is why calling this a leak and stopping would
miss the point.

There is a second cost. Both structural sites touch the full secrets dict, so
they sit inside the `no_log` blast radius of a structure whose values they
never actually read.

## Decision

**1. Publish the name list as its own fact.** The module already returns a
`names` list — sorted and explicitly safe to log — and
`tasks/load-bws-secrets.yml` already prints it in its "Report which secrets
were loaded" debug task. It is simply never exposed as a fact. Publish it as
`secret_names` alongside the values.

**2. Rewrite the two structural sites against `secret_names`.** The
`select('match', '^k3s_tls_sans_')` filter and the `reject('in', …)`
membership test both work unchanged on a plain list, and read more honestly
there than they do over a dict. Those tasks then never touch a secret value at
all, so they leave the `no_log` blast radius entirely.

**3. Broker the two plain leaks.** Add `proxmox_ssh_addr` and
`proxmox_ssh_user` to `vars.yml` and repoint `hosts.yml` and
`flatcar_vm/tasks/main.yml` at them.

**4. Rename the fact `bws` → `secrets`, and `tasks/load-bws-secrets.yml` →
`tasks/load-secrets.yml`.** The fact names a *vendor*; what consumers depend on
is that it is the secret store. Nothing currently binds `secrets` as a
variable. As its own commit, mechanical, no behaviour change.

After this the store is reachable from exactly two files — the module and the
load task — and that is a property a reviewer can check with one grep rather
than a convention everyone has to remember.

## Alternatives rejected

- **Leave it; four sites is not many.** It is not the count, it is that two of
  them *cannot be written correctly*. The `k3s_tls_sans_*` guard exists
  precisely because silent failure is the expensive kind here; leaving its only
  correct implementation reaching through the broker guarantees the next such
  guard does the same.
- **Broker the presence checks as boolean variables** (`ceph_mons_present`,
  one per secret). Works for the five known ceph values, but not for
  `k3s_tls_sans_*`, whose key set is *open* — it is derived from whatever is in
  the store, which is the entire point of that assert. It would also add a
  `vars.yml` entry per secret purely to answer "does this exist", inverting the
  broker's job.
- **Expose the whole dict under a neutral alias and call it brokered.** A
  rename with no property behind it. The value of the split is that
  `secret_names` is *safe to log*, which the values dict never is.
- **Fold this into the store-migration ADR.** ⚠ Deliberately not: this is
  correct whether or not [ADR-0032](0032-secrets-store-1password-migration.md)
  ever happens, and burying an independently-good change inside a much larger
  contested one is how it gets reverted along with it.
- **Rename the fact to the new vendor** (were ADR-0032 to proceed). That
  re-encodes a vendor name for the second time in about a year. `secrets` is
  intended to be the last rename.

## Consequences

- **The next store change is a two-file change**, not a ~40-site sweep. That
  is the main reason to do this *before* ADR-0032 is decided rather than as
  part of it — and it lets both backends run in parallel behind one fact name
  during any cutover.
- **~40 sites move in the rename**, almost all inside `vars.yml`. It must be
  its own commit with no behaviour change, or the diff of whatever comes next
  becomes unreviewable.
- **Two tasks stop handling secret values**, which is a small real reduction in
  how far `no_log` has to reach.
- **`secret_names` is safe to print** and becomes the natural thing to echo
  when a `{{ secrets.something }}` comes back undefined — the question the
  existing debug task already answers, now available to asserts too.
- ⚠ **The dict-iteration idiom disappears from `preflight.yml`.** That is a
  readability gain, but it is also the kind of subtlety that gets
  reintroduced; the reason it is wrong belongs in a comment at the new call
  site, not only here.
- ⚠ **Nothing about the fetch changes** — still one bulk call per play, still
  `run_once` + `delegate_to: localhost`, still `no_log` on the values. ADR-0027
  is refined, not reversed.

## Evidence

The four leak sites and their reference counts were established by grep over
`*.yml` / `*.yaml` / `*.j2` on 2026-09-12, excluding `vars.yml` and the load
task themselves; the ten live references were as tabulated above, the remaining
matches being comments.

Implemented 2026-09-12 in two commits, the seal and then the rename, per the
"its own commit" requirement above. Verified against the live store (29
secrets):

- **`render-frr-config.yml` and `render-ceph-setup.yml` produce byte-identical
  output**, checked after the seal and again after the rename against a
  baseline captured before either. Both re-runs also report `changed=0`, which
  is the same claim from the file modules' side.
- **The `k3s_tls_sans_*` assert still FIRES.** Poisoned via
  `-e '{"secret_names":[…,"k3s_tls_sans_ghost"]}'` (extra-vars outrank the
  `set_fact`), the guard failed naming `ghost`, and aborted inside preflight
  with `changed=0` — before any Proxmox call. Re-confirmed after the rename.
  Per this repo's standing rule, the negative test is what was watched, not
  the positive pass.
- **The new name-list gates agree with the old dict gates** on every live
  secret: `secret_names` sorted, equal to `secrets.keys()`, and both ceph
  presence gates returning the same answer as the `is defined` forms they
  replaced.
- All seven playbooks pass `--syntax-check`.

⚠ `grep -rn 'bws' ansible/` does **not** yet come back clean, and that is
expected at this point: the `bws_*` connection variables, the `bws_secrets`
module and the `bws_fetch` register still name Bitwarden deliberately (see
Decision 4). They go with the store itself, not with this record.
