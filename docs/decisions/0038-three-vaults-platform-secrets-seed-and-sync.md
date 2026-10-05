# ADR-0038: Three vaults by lifecycle and reader; ESO keeps the Ansible-seeded platform Secrets current from the same vault

- **Date:** 2026-10-04
- **Status:** Accepted — implemented in the repo; live verification pending the 1Password setup (`../worklog.md`)
- **Supersedes / related:** **partially supersedes [ADR-0034](0034-secrets-store-1password.md) — its vault layout only** (two vaults → three; the `op` CLI, grouped fields, desktop-app auth and the rate-limit analysis all stand). **Decides the "does ESO adopt the bootstrap-seeded Secrets?" open question** that ADR-0009 and ADR-0034 left for this milestone. Keeps [ADR-0026](0026-per-cluster-derivation-from-index.md)'s per-cluster blast-radius rule and [ADR-0033](0033-secrets-fact-broker.md)'s single-loader seal. Related: [ADR-0021](0021-topology-blinding-postbuild-substitution.md) (`cluster-topology` is unaffected — still Ansible-seeded permanently), [ADR-0035](0035-site-scope-multiple-proxmox-clusters.md) (the zone question decides how the Cloudflare token forks at cluster #2), [ADR-0040](0040-whether-infrastructure-config-splits-per-domain.md) (the tier-gating consequence). Code: `ansible/playbooks/tasks/load-secrets.yml`, `ansible/inventory/group_vars/all/vars.yml`, `ansible/playbooks/bootstrap-cluster.yml`, `gitops/infrastructure/external-secrets/`, `gitops/infrastructure-config/external-secrets/`, `ansible/SECRETS.md`.

## Context

ADR-0034 split 1Password by consumer: `homelab-infra` for the control node,
`homelab-apps-<cluster>` for that cluster's ESO. It left one question for the
ESO milestone: once ESO is live, does it **adopt** the Secrets Ansible seeds
for in-cluster consumers (cert-manager's Cloudflare token, ceph-csi's two cephx
keys), or do they stay seed-only? The recorded lean was "adopt, keep the seed".

Taken literally, that lean puts each such credential in **two vaults**. ESO
cannot read `homelab-infra` (that is the point of the split), so to adopt the
token it would read a copy in `homelab-apps-<cluster>`, while Ansible kept
seeding from the original. Two sources of truth for one credential. A rotation
would have to update both, and the seed would silently undo a half-done one on
the next Ansible run. Adoption would then buy no rotation benefit while adding
a drift hazard.

Seed-only avoids the duplication but leaves rotation as "edit 1Password, then
re-run `bootstrap-cluster.yml`" for secrets that do rotate, the Cloudflare
token most of all.

The vaults actually hold three kinds of secret, each with its own lifecycle:

1. **Bootstrap-only**: sensitive values that are never needed in-cluster:
   the Proxmox API token, SSH keys, k3s join tokens, the FRR password, and the
   ESO service-account tokens themselves.
2. **Bootstrap, but long-lived in-cluster and rotated over time**: the
   Cloudflare DNS-01 token and the cephx keys. These are needed before ESO
   exists on a from-scratch rebuild, and needed current for the life of the
   cluster.
3. **App secrets**: ordinary runtime secrets that only ESO ever needs.

The old two-vault layout forced kind 2 into one side or the other. That forced
choice is the source of the duplication.

## Decision

### Three vaults, by who reads them

| Vault | Scope | Holds | Read by |
|---|---|---|---|
| `homelab-infra` | fleet-wide | kind 1 — bootstrap-only | control node only |
| `homelab-platform-<cluster>` | **per cluster** | kind 2 — bootstrap + rotated (`cert-manager` item: `cloudflare_api_token`, plus `base_domain` by the scope rule below; `ceph-csi` item: `ceph_k8s_rbd_key`, `ceph_k8s_cephfs_key`) | control node **and** that cluster's ESO |
| `homelab-apps-<cluster>` | per cluster | kind 3 — app secrets | that cluster's ESO only |

Every kind-2 secret lives in **exactly one place**, the platform vault, and
both writers read it there. They therefore agree by construction, and a
rotation is one edit in 1Password.

- **Per-cluster, not fleet-wide**, for the reason ADR-0026 gives for join
  tokens: a compromised cluster must not reach another cluster's cephx keys.
  "Platform" names the scope (the platform controllers' own secrets, as
  opposed to apps), following ADR-0034's `homelab-` + scope + (cluster)
  naming rule.
- **The ESO service-account token stays in `homelab-infra`** (item
  `eso-service-accounts`, field `eso_op_service_account_token_<cluster>`).
  The thing that grants access cannot live behind the access it grants.

### Placement rule: a value lives in the vault matching its SCOPE, not its delivery

The circularity (a value read before ESO's tier applies can never come from
ESO) constrains how a value **reaches the cluster**: `cluster-topology` or an
ExternalSecret. It does not constrain where the value is **stored**. So the
test for which vault a field belongs in is its scope:

- **`base_domain` goes with `cloudflare_api_token`** in the platform
  `cert-manager` item, although ESO never reads it (it still reaches the
  cluster via `cluster-topology`). The two are one scope, the DNS zone, and
  must fork together (ADR-0035); a zone-scoped token cannot issue for another
  zone. One item makes that structural, and per-cluster storage also settles
  the deferred "two clusters can't both own `*.<base_domain>`" problem: each
  cluster's vault carries its own.
- **Ceph topology (`ceph_fs_name`, `ceph_mons`, `ceph_fsid`) stays in
  `homelab-infra`.** It describes the Proxmox Ceph, which every k8s cluster on
  that site shares (ADR-0035's *site* scope). A per-cluster vault would hold one
  copy per cluster. `render-ceph-setup.yml` also needs `ceph_fs_name` before
  any platform item can exist. The fleet vault stands in for site scope until
  that scope is built.

### Seed and sync are permanent partners: `creationPolicy: Merge`

- **Ansible creates** each platform Secret at bootstrap, from the platform
  vault, exactly as before. A from-scratch rebuild still has certificates and
  storage before ESO exists.
- **An `ExternalSecret` with `creationPolicy: Merge` keeps it current** from
  the same field. Merge never creates the Secret, never deletes it, takes no
  ownerReference, and writes only the keys it lists. So Ansible owns the
  Secret's existence and shape (including the non-secret cephx `userID`),
  and ESO owns the value's freshness. `deletionPolicy: Retain` keeps the last
  good value if the field ever disappears.
- `refreshInterval: 24h`: these rotate rarely and by hand, so the periodic
  refresh is only a drift backstop. A rotation is followed by a `force-sync`
  annotation rather than a wait. Cost is 3 calls/day.

### One ESO service account per cluster, granted two vaults; two stores

- The account is granted `read_items` on **exactly** `homelab-platform-<cluster>`
  and `homelab-apps-<cluster>`. The grant is immutable (ADR-0034), so
  `bootstrap-cluster.yml` runs `op vault list` *as that token* before seeding
  it and refuses unless it sees exactly those two vaults. That one call also
  defends against
  [external-secrets#4925](https://github.com/external-secrets/external-secrets/issues/4925):
  a bad token fails loudly at seed time instead of exhausting the
  account-wide daily quota from inside the cluster.
- **Two ClusterSecretStores**, because a store names exactly one vault:
  `onepassword-platform`, restricted by `conditions.namespaces` to
  `cert-manager` and `ceph-csi`, and `onepassword-apps`, unrestricted. The
  restriction is a second boundary *inside* the cluster: an app namespace
  cannot write an ExternalSecret that pulls the Cloudflare token.

### Loading the platform vault on the control node

Every cluster's platform vault has the same layout, so field labels repeat
across clusters. They stay **plain in 1Password** (ESO's remote refs read them
as `cert-manager/cloudflare_api_token`), and the loader **suffixes them
`_<cluster>`** on read. That gives `cloudflare_api_token_testnode`, the same
convention as `k3s_token_<cluster>`, and keeps the global label-uniqueness
assert meaningful. Loading is opt-in per play (`op_load_cluster_vaults: true`),
because it needs `clusters` in scope and costs one call per item per cluster.
Only `bootstrap-cluster.yml`'s seeding play sets it. The loader stays the only
file that reads secrets (ADR-0033); the ESO-token check is the one other store
call, because it must run *as* that token and checks a credential rather than
reading a secret.

A new assert **refuses a platform secret that also exists in `homelab-infra`**.
That catches the half-done move: the value copied into the platform vault but
the old field left on `control-node`. Without it, the suffix would keep the two
labels apart and the stale copy would linger, Ansible reading one copy and ESO
the other.

## Alternatives rejected

- **Seed-only (no ESO involvement for kind 2).** No duplication, but rotation
  stays a manual Ansible run per cluster, and nothing in-cluster ever notices a
  stale value.
- **Adopt from `homelab-apps-<cluster>` (the recorded lean).** Duplicates every
  kind-2 credential across two vaults. See Context.
- **Fold kind 2 into `homelab-apps-<cluster>` and let the control node read
  it.** One vault fewer, but the control node (and any future CI or Linux
  service account) would gain read access to every app secret it never needs,
  and the in-cluster namespace boundary between platform and app secrets would
  disappear with the store split.
- **A fleet-wide `homelab-platform` vault.** Simpler while there is one
  cluster, but a compromised cluster could read the others' cephx keys. This is
  the same argument ADR-0034 accepted for the apps side.
- **Two ESO service accounts per cluster (one per store).** Each token would
  reach one vault, but both tokens would sit in the same namespace, readable by
  the same controller, so the split buys little for twice the seeding and
  rotation.
- **`creationPolicy: Owner` or `CreateOrMerge`.** Either lets ESO create the
  Secret alone, without `userID` in the cephx case. Owner also ties the
  Secret's life to the ExternalSecret's (and so to ESO's CRDs).
- **A provider `cache:` block on the platform store.** It would serve a
  post-rotation `force-sync` from memory for up to its TTL.

## Consequences

- **The adoption open question is closed.** The answer is "keep the seed, and
  sync from the same vault", which neither of the two recorded options was.
- **ADR-0034's vault layout is partially superseded.** The Cloudflare token,
  `base_domain` and the cephx keys **move out of** the `control-node` item into
  `homelab-platform-<cluster>`, and the loader enforces that move. Ceph
  topology stays (see the placement rule).
- **New `cluster-topology` key `cluster_name`.** The shared manifests need the
  vault name per cluster. It is not topology (it is already public as
  `gitops/deployment/<cluster>/`), but this Secret is the only way a shared
  manifest gets a per-cluster value. ⚠ It must be seeded **before** the gitops
  change reaches the cluster, or strict substitution fails
  `infrastructure-config`.
- **The platform ExternalSecrets have real Ready conditions**, so
  `infrastructure-config`'s `wait: true` now also gates `apps` on 1Password
  being reachable, though the seeded Secrets keep working through an outage.
  Taken up in ADR-0040.
- ⚠ **At cluster #2**, two values still need a decision. The `base_domain` +
  Cloudflare token pair takes the DNS-zone shape (ADR-0035); that is a vault
  edit either way, since both are already per-cluster. The cephx users
  (`k8s-rbd`, `k8s-cephfs`) are fleet-wide names today; a second cluster on
  the same Ceph should get its own cephx users and caps rather than a copy of
  the key.
- **Failure cost is set by controller-runtime, not by this design.** ESO has
  no backoff knob. A failing object retries at a 1000 s cap, forever (~86
  calls/day each), and `refreshInterval` does not apply to failures. The
  flood gate limits a bad token to the 2 stores; a broken remote ref with a
  good token is not limited. The table and the emergency brake are in
  `gitops/CLAUDE.md`.
- **Per-app isolation inside a cluster** (namespace-scoped stores, or per-app
  conditions on `onepassword-apps`) is deferred until `apps/` has more than one
  tenant.

## Evidence

Loader behaviour verified offline on 2026-10-04 against a stub `op` serving
fixture items. Values arrive suffixed. A leftover `cloudflare_api_token` on
`control-node` fails the single-home assert, which shows the negative case
fires. A missing platform item fails with the platform-vault hint. The
opt-out path reads only the fleet items. Live verification (store Ready, a
Merge sync observed on each platform Secret, the grant assert run against the
real token) is recorded in `../worklog.md` once the vaults exist.
