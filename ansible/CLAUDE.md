# CLAUDE.md — ansible/ (provisioning, cluster bootstrap, Flux handoff)

Nested notes for work inside `ansible/`. Loads automatically when Claude reads
a file in this subtree. Project-wide facts (network roles, secrets tiers,
guardrails) are in the root `CLAUDE.md`; the design is
`../docs/architecture.md`; **why** anything is the way it is lives in
`../docs/decisions/` (cited as ADR-NNNN); what happened when, with evidence, is
`../docs/worklog.md`. This file holds what you need to *work here* without
re-learning it: the current state, what's next, and the non-obvious facts.

How to run and verify anything: `README.md` in this directory.

## Current state and next step

**Everything `site.yml` does is live and verified on a from-scratch run
(2026-08-30)** on the single all-in-one node `snoop-a2o`: template → Flatcar VM
shell → k3s server → Calico (v3.32.1, eBPF, BGP no-encap, LB IPAM + #12890
workaround, pfSense session `Established`, LB IPs reachable) → Flux
(operator + sync-less `FluxInstance`, cosign-verified OCI source, all tiers
Ready, Calico adopted with no diff war). Evidence: worklog entries for
2026-08-16, 2026-08-29 and 2026-08-30.

**Delivered from Git so far (both verified live, worklog 2026-09-05):** NGINX
Gateway Fabric (Gateway API CRD tier, shared Gateway, LB reachable,
source-IP preserved) and cert-manager (DNS-01 wildcard issued, HTTPS on the
Gateway, `infrastructure-config` tier). **Next: ceph-csi-operator +
StorageClasses** → ESO + Bitwarden SDK Server → Postgres + Redis → LiteLLM →
… That work is in `gitops/`; this directory's part is seeding the
bootstrap-tier secrets as they arrive — the cert-manager DNS-01 token (done,
in `bootstrap-cluster.yml`) and the ESO access token when that milestone
comes. ⚠ Do **not** fold "stop vendoring the Calico
CRDs" into it — `bootstrap-cluster.yml` primes from the vendored file, so that's
its own step (ADR-0020).

After that, here: the control-plane taint, the agent/worker join path (only the
server path is built), and the Mac role. Node 2's join is a **dataplane event**
(ADR-0024, ADR-0018), not a capacity add.

## What the plays do

`site.yml` imports four plays in dependency order; each is also runnable alone.

| Play | Owns | Needs |
|---|---|---|
| `build-template.yml` | Flatcar proxmoxve image → import → template (vmid 9000). ⚠ Guarded on `qm status` failing, so it **runs green and silently skips** whenever the template exists; a successful run is not evidence of a fresh template (ADR-0030). | BWS |
| `provision-nodes.yml` | per node: render Butane → `butane --strict` → upload `.ign` (SSH) → clone + pin MACs + disk + `cicustom` (API) → boot → wait for SSH → detach `cicustom` then delete the `.ign` (ADR-0025) | BWS |
| `bootstrap-cluster.yml` | per cluster: wait for `/readyz`, fetch + rewrite the kubeconfig to `.kube/<cluster>.config`, seed `cluster-topology` + the cert-manager `cloudflare-api-token` Secret (bootstrap-secret tier), server-side-apply the vendored CRDs, `helm` the tigera-operator from `gitops/infrastructure/calico/values.yaml`, apply the BGP CRs + #12890 workaround + endpoint ConfigMap via `flux build kustomization --strict-substitute`, wait Ready | BWS, `helm` |
| `flux-bootstrap.yml` | helm-install the flux-operator (`flux_operator_version`), apply ONE sync-less `FluxInstance` with the `StrictPostBuildSubstitutions` patch, assert the gate landed, seed `gitops/deployment/<cluster>/{source,sync}.yaml`, wait for `flux-system`/`crds`/`infrastructure`/`apps` Ready | the previous play's kubeconfig + Secret; **no credentials** |
| `render-frr-config.yml` | pfSense/FRR raw config + firewall-alias members → `.frr/` (git-ignored), from the node map; asserts index/ASN/LB-range collisions **and its asserts are verified to fire** | BWS |

Every play that reads a `{{ bws.* }}` value includes
`tasks/load-bws-secrets.yml` first — one bulk API call into the `bws` fact
(ADR-0027). `tasks/load-node-map.yml` flattens `inventory/nodes.yml`'s
`clusters` into a cluster-annotated `nodes` map and asserts **global**
hostname/`node_number` uniqueness.

**The dual-applied set is deliberately small** (ADR-0016): Calico's values, the
vendored CRDs, the BGP CRs, the #12890 workaround, the endpoint ConfigMap, and
the Flux root. Everything after Flux exists is Flux-only. `flux build` is the
tool when priming a substituted manifest, never the default posture.

## Node map and derivation

`inventory/nodes.yml` is the **sole source of truth** for node identity
(ADR-0017, ADR-0026). Nodes sit under the k3s cluster they belong to; **the
cluster key IS the cluster's name** — it names the kubeconfig cluster/user/
context, the file `.kube/<cluster>.config`, and the Flux entrypoint directory
`gitops/deployment/<cluster>/`. Rename one without the others and Flux points at
a path that doesn't exist.

Everything host-shaped derives from `node_number` (DMZ IP, Ceph IP, both MACs,
vmid) and everything cluster-shaped from `index` (ASN = `bgp_asn_base + index`,
LB range = `<lb_range_base>.<index>.0/24`). Per-cluster overrides:
`k3s_version` (`k3s_minor` is derived; preflight asserts minor ⊂ version);
per-cluster BWS secrets `k3s_token_<cluster>` / `k3s_tls_sans_<cluster>`.
`calico_version` is deliberately **fleet-wide** — it's dual-owned with `gitops/`
(ADR-0019).

⚠ The neighbor address in `render-frr-config.yml` and `eth0_ip` in
`roles/flatcar_vm/tasks/preflight.yml` are the **same derivation in two
places**. Change both or pfSense peers with addresses no node holds.

## Non-obvious facts: Flatcar, Ignition, k3s

These were each learned the hard way (worklog 2026-07-07 onward). They are
rules, not history.

- **Ignition's files stage runs in the initramfs, which has no network here.**
  No DHCP, and the static `eth0` config only activates after the pivot — so a
  remote `contents.source:` hangs and the node **boot-loops** (console dead-ends
  in the initramfs disk stage, never reaches `Welcome to Flatcar`). The k3s
  sysext is therefore downloaded post-pivot by `k3s-sysext-download.service`
  (`After=network-online.target`, `Condition`-guarded on the `.raw` path so it
  runs once). Ignition must not create the `/etc/extensions/k3s.raw` symlink
  either — a dangling symlink at the early sysext merge can break the
  docker/containerd sysexts; the download unit owns it.
- **The k3s sysext ships no auto-enable drop-in** and its `k3s.service` lives
  *inside* the sysext, absent at Ignition time. It's enabled with a
  `storage.links` wants/ symlink, **not** `systemd.units[].enabled` (which would
  try to enable a unit that doesn't exist yet). `k3s.service` gets
  `After=systemd-sysext.service` + `RequiresMountsFor=/var/lib/rancher` via a
  drop-in.
- **`systemd-sysupdate` naming embeds the minor**: the feature is `k3s-<minor>`,
  the transfer conf is `/etc/sysupdate.k3s-<minor>.d/`, `MatchPattern` is
  `k3s-<minor>.@v-%a.raw`, and the update runs as
  `systemd-sysupdate -C k3s-<minor> update` from an `ExecStartPre` drop-in on
  the base `systemd-sysupdate.service` (which otherwise only updates the OS).
  All three names must match. A patch lands on the next boot
  (`/run/reboot-required`), not hot-swapped. **Consequence:** `k3s_version_default`
  is the *seed* for a fresh node; a long-running node drifts within the minor.
  That's expected, not drift. Re-verify with `ls /opt/extensions/k3s/`,
  `ls -la /etc/extensions/`, `journalctl -u systemd-sysupdate | grep k3s`.
- **If the seed version falls outside the sysupdate `MatchPattern`, the node
  boots the right k3s and then silently never updates.** Preflight asserts
  minor ⊂ version on both the derived and override paths for exactly this.
- **The data disk is mounted at `/var/lib/rancher` — k3s's default data-dir
  root — not pointed at by a `data-dir` override.** An override broke
  `k3s secrets-encrypt`, `etcd-snapshot`, the uninstall script and community
  tooling, all of which assume the default path. Mounting *at* the default
  sidesteps all of it.
- **Flatcar nodes have no Python.** Anything that runs *on* a node uses `raw`
  with `sudo` embedded in the command — never `command`/`slurp`/`copy`. All
  k8s-side work runs from `localhost` against the cluster via the kubeconfig;
  nothing k8s is installed on the node.
- **Talos guidance does not transfer.** "Immutable OS" advice (e.g. Calico's
  `cgroupV2Path` override, calico#7892) is Talos-specific; Flatcar makes only
  `/usr` read-only and `/run` is an ordinary tmpfs. Verify against the node,
  don't copy.
- **`ide2` (the cloud-init drive) stays attached** after the snippet is
  destroyed; with `cicustom` gone PVE generates an inert default config for it
  carrying nothing sensitive. It's standard, it comes back with every clone, and
  `cicustom` needs it at the next rebuild.
- **The kubeconfig rewrite exists because k3s names cluster, user and context
  all `default`**, pointing at `127.0.0.1`. `bootstrap-cluster.yml` renames them
  to `<cluster>` / `<cluster>-admin` / `<cluster>` and repoints `server:` at the
  DMZ IP, writes `.kube/<cluster>.config` (0600, git-ignored), and merges into
  `~/.kube/config` when `kubeconfig_merge_user`. Knobs in `group_vars/all/vars.yml`.

## Non-obvious facts: Proxmox

- **API for lifecycle, SSH only for the snippet file.** `cicustom` attach *and*
  detach go through `proxmox_kvm`; the `.ign` upload/delete are file operations
  with no API, so they go over SSH as `provisioner` with sudo scoped to `qm`
  and the snippet-dir repair. Prefer the API for anything new.
- **PVE resets the snippets directory to `root:root 0755` on storage
  activation** — a template rebuild, or merely deleting a file in it, is
  enough. `flatcar_vm` stats, repairs (scoped `chgrp`/`chmod` sudoers rules,
  **per PVE node**, fixed arguments that must match `proxmox_snippet_*`
  byte-for-byte), re-stats and asserts. Ansible's `copy` checks the *directory*
  only when the destination file doesn't exist, which is why a leftover
  snippet hides the problem until the next new node.
- **Detach `cicustom` BEFORE deleting the snippet.** PVE regenerates the
  cloud-init drive on every start and `read_cloudinit_snippets_file` has no
  error handling — a dangling reference fails at the node's **next reboot**,
  not at provision time. `tasks/destroy-ignition-snippet.yml` enforces the order.
  Restoring a VM backup taken before the cleanup hits the same trap; fix with a
  re-provision or `qm set <vmid> --delete cicustom`.
- **SDN.Use is required on PVE 8.x even for plain bridges** — the clone copies
  the template's `net0`, which checks `/sdn/zones/localnetwork/<bridge>`.
- **Token privsep must be OFF** (`--privsep 0`) or the token has an empty ACL
  and everything 403s despite the role.
- **macOS Local Network Privacy blocks Python (not `nc`/`curl`) from the PVE
  API on the same link.** The valid test is Python vs an Apple-signed binary
  against the **same on-link host** — a routed host proves nothing. Diagnostic
  in `README.md` → Troubleshooting.

## Non-obvious facts: Flux bootstrap

- **`--feature-gates` MERGES, it does not override.** The operator already
  emits `--feature-gates=ObjectLevelWorkloadIdentity=false`; appending ours is
  correct and version-proof. The gate assert folds every `--feature-gates`
  argument into one map, last writer wins *per key*.
- **Flux reads the artifact, not your working tree.** `kubectl kustomize` on
  disk proves nothing about what's published. The symptom of a path that
  exists locally but not in the artifact is **misleading**: the source goes
  Ready (the pull worked) and only the `flux-system` Kustomization fails. Paths
  inside the artifact are `gitops/`-relative — no `./gitops` prefix.
- **The artifact must be rebuilt from `main` before the play runs**; Flux
  pulls the signed artifact, not the branch. Merge → CI signs → Flux picks it
  up.
- **The play refuses to convert a sync-based `FluxInstance` in place** —
  stripping `spec.sync` would prune the generated `flux-system` Kustomization,
  whose `prune: true` cascades to all tiers and **uninstalls Calico**.
  Migration is by re-provision.
- **To get back to a pre-Flux cluster, roll back a Proxmox snapshot. Do NOT
  delete the `FluxInstance`.** The operator's own inventory + a
  `fluxcd.controlplane.io/finalizer` do the deleting — not ownerReferences (the
  generated objects carry none) — so `kubectl delete --cascade=orphan` does not
  save you. A rollback with RAM also preserves the kubeconfig and the
  `cluster-topology` Secret, so `bootstrap-cluster.yml` needn't re-run.
- **The operator is Ansible-owned and not primed for adoption**; nothing in
  `gitops/` manages it. Self-managing the operator is a real pattern and a real
  footgun (an in-flight upgrade can delete the controller performing it).
  Adopt it deliberately or not at all.
- **In-cluster Flux never reads a kubeconfig.** `spec.kubeConfig` exists only
  for reconciling a *remote* cluster; each controller authenticates with its
  own ServiceAccount. Don't re-raise "a scoped kubeconfig for Flux" as a
  blocker — it's a category error. Flux's in-cluster RBAC (cluster-admin today)
  is a separate lever to revisit once `apps/` has workloads worth isolating.

## Ansible traps specific to this repo

Each of these produced a *silent-wrong* result, not a loud failure.

- **`set_fact` persists across the role's per-node loop.** A fact set under
  `when:` leaves a non-matching node holding the *previous* node's value. Set
  cluster-scoped facts unconditionally (verified: a bare VM after two k3s nodes
  resolves to `''`, not a stale token).
- **Facts outrank task vars.** A cleanup task reusing `hostname`/`vmid`/
  `eth0_ip` would silently get the *last* provisioned node's values and detach
  `cicustom` from the wrong VM. Hence every var in
  `tasks/destroy-ignition-snippet.yml` is `cleanup_*`-prefixed, backed by an
  "exactly one VM and its name == this node" assert.
- **A skipped task still registers** — as `{changed, skipped, skip_reason}`
  with no `stat` key. A conditional re-stat that reuses the original
  `register:` name wipes the good result on the happy path. Register under a
  new name and `default()` back. *A fix verified only on the failure it was
  written for is half-verified.*
- **A task-level `vars:` entry referencing `item` does not re-resolve per
  iteration** under this ansible-core's lazy templating — a scanner over 18
  files produced an empty list. Use the repo's single-expression Jinja-loop
  idiom (see `k3s_tokens`, `cluster_bgp` in `vars.yml`).
- **Keys inside one `set_fact` are not resolved in declaration order**; an
  earlier key referencing a later sibling fails. Inline instead.
- **`proxmox_kvm` hard-codes `changed=True` on `delete`.** Gate on the current
  config to keep the task honest.
- **Ansible `copy` checks the directory's writability only when the
  destination file doesn't exist.** See the snippet-dir note above.
- **Verify a negative test fires**, not just that the positive passes — the
  collision asserts in `render-frr-config.yml` and the sudoers rules were both
  checked that way.

## Open items

- **Kubeconfig hygiene on the control node.** `bootstrap-cluster.yml` leaves a
  long-lived cluster-admin cert at `.kube/<cluster>.config` and merged into
  `~/.kube/config`. Deleting it in the happy path is wrong (standalone plays
  need it; `kubeconfig_merge_user: false` would leave no kubeconfig anywhere).
  Options if we act: an opt-in `kubeconfig_cleanup_local` flag, or a deliberate
  `clean-kubeconfig.yml` — never a silent step in provisioning.
- **Flatcar OS update policy** is unset (ADR-0030, Open) — nodes auto-update
  and reboot on their own schedule. Record OS + kernel with any test result.
  Cheap mitigation not yet done: a `flatcar_template_force` flag so a template
  rebuild isn't a manual `qm destroy 9000`.
- **`MatchPattern=` in `[Target]`** of `k3s-sysupdate.conf.j2` — sysupdate logs
  a harmless "lacks MatchPattern" warning each run; adding it explicitly
  silences it and drops the reliance on a default.
- **Agent/worker join path** is not built; server-only flags in
  `k3s-config.yaml.j2` must not reach an agent config.
- **Tests still owed:** the real API blip during a Proxmox HA restart of the CP
  VM; a ceph-csi PVC (RBD + CephFS) against this Ceph release and the Flatcar
  kernel's image features.
- **Drop Helm for Calico** (ADR-0029, Proposed) — would delete the adoption
  problem, `helm` as a control-node prerequisite, and the `kubernetes.core`
  Helm-major pin. Not before the next milestone has a known-good cluster to
  diff against.

## Guardrails specific to this directory

(Repo-wide guardrails live in the root `CLAUDE.md`.)

- Keep `cluster-cidr` / `service-cidr` pinned explicitly in every node's config;
  `bootstrap-cluster.yml` asserts Calico's IPPool equals `k3s_cluster_cidr`.
- Server-only k3s flags do not go on agent/worker join configs — workers get
  only the server URL + token.
- `eth0` (DMZ) and `eth1` (Ceph public) are the only two networks a node
  touches. `eth1` is **tagged**, has **no `Gateway=`**, and has MTU `8996` set
  explicitly at both the Proxmox NIC and the networkd unit.
- `.ign` files are `0660` in a `2770` root:pve-snippets directory — they embed
  the join token — and are deleted after first boot. Never widen either.
- Nothing under `.kube/`, `.frr/`, or `*.ign` is ever committed.
