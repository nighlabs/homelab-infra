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
