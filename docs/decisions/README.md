# Decision log

One record per decision: the context, what was decided, **every alternative
rejected and why**, and the consequences. This is where the reasoning survives
after the conversation that produced it is gone. **Read the relevant record
before re-litigating a choice that looks arbitrary — it almost certainly isn't.**

Rules:
- A record is never edited to change the past. A reversal is a **new** ADR
  that supersedes the old one; the old one's status line points forward.
- Present tense in the decision; dates in the header. Evidence lives in
  [`../worklog.md`](../worklog.md) — link it, don't duplicate the tables.
- Statuses: **Accepted** (in force), **Accepted — not yet implemented**,
  **Proposed** (argued for, not done), **Open** (question with no decision),
  **Superseded by …**, **Partially superseded by … (which part)** — for a
  record with independent layers where only one is replaced (0009), and
  **Answered by …** — for an Open question a later record decides, where the
  investigation is worth keeping (0032).
- Blinding as everywhere else: `${placeholder}` / `x.x.x.N`, never a real
  address.

## Index

### Initial design (2026-07)

| # | Decision | Status |
|---|---|---|
| [0001](0001-native-inference-on-the-mac.md) | Inference runs natively on the Mac Studio; everything else runs in Kubernetes | Accepted |
| [0002](0002-vllm-mlx-behind-llama-swap.md) | Inference engine: vllm-mlx per model behind llama-swap | Accepted |
| [0003](0003-k3s.md) | Kubernetes distribution: k3s | Accepted |
| [0004](0004-cluster-shape-kine-single-cp-proxmox-ha.md) | Cluster shape: SQLite via kine, one tainted control plane with HA from Proxmox, 1 CP + 3 workers | Accepted (one all-in-one node so far) |
| [0005](0005-flatcar-k3s-sysext-ignition-config-drive.md) | Node OS: Flatcar with the k3s sysext; Ignition via the cloud-init config drive | Accepted |
| [0006](0006-ceph-csi-external-proxmox-ceph.md) | Persistent storage: ceph-csi-operator against the existing Proxmox Ceph | Accepted, verified (2026-09-07) |
| [0007](0007-ansible-not-terraform.md) | Provisioning: Ansible only — Terraform/OpenTofu dropped | Accepted |
| [0008](0008-flux-via-flux-operator.md) | GitOps: FluxCD via the Flux Operator, bootstrapped by Ansible last | Accepted (source detail superseded by 0028) |
| [0009](0009-secrets-aescbc-and-eso-bitwarden.md) | Secrets: k3s secrets-encryption at rest; ESO + Bitwarden Secrets Manager for app secrets | **Partially superseded by 0034 (layer 2 only)** — layer 1 unchanged and live; ESO half now 0034 + 0038 |
| [0010](0010-calico-over-cilium.md) | CNI: Calico | Accepted |
| [0011](0011-cluster-cidrs-never-cgnat.md) | Cluster CIDRs live in `10.0.0.0/8` — never CGNAT | Accepted |
| [0012](0012-metallb-bgp.md) | Load balancer: MetalLB in BGP mode | **Superseded by 0018** |
| [0013](0013-ingress-certs-dns-external-access.md) | Edge: NGINX Gateway Fabric, cert-manager DNS-01 wildcard, split-horizon DNS, Cloudflare Tunnel + Tailscale | Accepted — ingress + certs implemented (2026-09-05); DNS/access not yet; internal resolver **Open** |
| [0014](0014-observability-managed-backend.md) | Observability: vendor-neutral collector in-cluster, managed backend out-of-band | Accepted — not yet implemented |
| [0015](0015-backups-nas-s3-and-break-glass.md) | Backups: NAS as S3 target; crown-jewels / break-glass | Accepted — not yet implemented |

### Implementation (2026-07-07 onward)

| # | Date | Decision | Status |
|---|---|---|---|
| [0016](0016-calico-ansible-primes-flux-adopts.md) | 2026-07-08 | Calico is installed once by Ansible, then adopted by Flux | Accepted, verified |
| [0017](0017-static-addressing-no-dhcp.md) | 2026-07-07 | All node addressing is static, rendered into Ignition from the node map; no DHCP | Accepted, verified |
| [0018](0018-calico-bgp-replaces-metallb.md) | 2026-08-02 | Calico BGP owns LoadBalancer IP allocation, advertisement, and the pod dataplane; no MetalLB | Accepted, verified — supersedes 0012 |
| [0019](0019-k3s-1.36-calico-3.32.1-version-pair.md) | 2026-08-02 | Pin k3s v1.36.x + Calico v3.32.1 as a pair; pre-apply the #12890 RBAC workaround | Accepted, verified — workaround **retired 2026-10-04** (never needed on v3.32.1) |
| [0020](0020-crd-tier-vendored-server-side-apply.md) | 2026-08-02 | A `crds/` tier: Calico's CRDs vendored and server-side applied, `prune: false` | Accepted; build-time render **open**; size rationale **corrected by 0039** |
| [0021](0021-topology-blinding-postbuild-substitution.md) | 2026-08-02 | Topology as `${var}` placeholders substituted from the `cluster-topology` Secret; SOPS only as fallback | Accepted, verified |
| [0022](0022-pfsense-frr-raw-config-explicit-neighbors.md) | 2026-08-02 | pfSense FRR as generated raw config, explicit `neighbor` statements | Accepted, verified |
| [0023](0023-rfc8212-real-policy-le32.md) | 2026-08-02 | RFC 8212 satisfied by real prefix lists with `le 32`, never disabled | Accepted, verified |
| [0024](0024-calico-ebpf-dataplane-no-kube-proxy.md) | 2026-08-02 | Calico eBPF dataplane; kube-proxy disabled | Accepted, verified |
| [0025](0025-destroy-ignition-snippet-after-first-boot.md) | 2026-08-03 | The Ignition snippet is destroyed after first boot (it embeds the join token) | Accepted, verified |
| [0026](0026-per-cluster-derivation-from-index.md) | 2026-08-16 | Cluster `index` → ASN + LB range; per-cluster token/SANs/version; LB range routed-only | Accepted, verified |
| [0027](0027-control-node-secrets-bws-runtime.md) | 2026-08-17 | Ansible reads BWS at run time; `vault.yml` retired; secret zero in the macOS Keychain | **Superseded by 0034** |
| [0028](0028-gitops-delivery-signed-oci-syncless-fluxinstance.md) | 2026-08-29 | Flux consumes a cosign-signed OCI artifact; sync-less FluxInstance; self-managed root | Accepted, verified |
| [0029](0029-drop-helm-for-calico.md) | 2026-08-02 | Install the tigera operator from manifests instead of the Helm chart | **Proposed** |
| [0030](0030-flatcar-os-update-policy.md) | 2026-08-02 | Flatcar auto-update/reboot policy | **Open** |
| [0031](0031-ceph-csi-operator-vendored-manifests-not-helm.md) | 2026-09-06 | ceph-csi-operator from vendored manifests, not its Helm chart (two CRD templates ~2× the client-side apply limit, no toggle) | Accepted, verified — its size premise is **corrected by 0039**, so the chart route is open again |
| [0032](0032-secrets-store-1password-migration.md) | 2026-09-12 | Whether to replace Bitwarden Secrets Manager with 1Password (ESO 1Password SDK provider, no in-cluster secrets server) | **Answered by 0034** — retained as the investigation |
| [0033](0033-secrets-fact-broker.md) | 2026-09-12 | `vars.yml` brokers secret *values*; name-space questions get a `secret_names` fact; the fact is `secrets`, not a vendor name | Accepted, verified |
| [0034](0034-secrets-store-1password.md) | 2026-09-12 | 1Password replaces BWS; Ansible reads it with the `op` CLI; grouped fields; the control node keeps **no secret zero** (desktop-app auth) | Accepted, verified (2026-09-13) — Bitwarden deleted; **vault layout partially superseded by 0038** |
| [0035](0035-site-scope-multiple-proxmox-clusters.md) | 2026-09-12 | A third scope — **site** (one Proxmox cluster + its Ceph) — between fleet-wide and per-k3s-cluster | **Open** (implementation) — but the naming rule, global `node_number` uniqueness, and DNS-zone scoping are **decided** |
| [0036](0036-whether-omlx-replaces-vllm-mlx-and-llama-swap.md) | 2026-10-04 | Whether oMLX (one daemon: continuous batching, tiered KV cache, multi-model LRU) replaces vllm-mlx + llama-swap on the Mac | **Open**, exploration; decide when the Mac tier is built (would supersede 0002) |
| [0037](0037-whether-llmkube-manages-mac-models.md) | 2026-10-04 | Whether LLMKube manages the Mac's models as Flux-delivered CRDs (operator in k3s, metal-agent on the Mac) | **Open**, exploration; after the cluster stack and 0036 (would partially supersede 0001 and 0013) |
| [0038](0038-three-vaults-platform-secrets-seed-and-sync.md) | 2026-10-04 | Three vaults by lifecycle and reader (`homelab-infra` / `homelab-platform-<cluster>` / `homelab-apps-<cluster>`); ESO keeps the Ansible-seeded platform Secrets current with `creationPolicy: Merge` | Accepted — live verification pending; partially supersedes 0034, decides the adoption question |
| [0039](0039-helm-installs-crds-over-the-apply-limit.md) | 2026-10-04 | The 262144-byte limit is client-side apply's annotation; Helm installs larger CRDs, so ESO's come from its chart | Accepted, verified (server dry-run); corrects 0020/0031's rationale |
| [0040](0040-whether-infrastructure-config-splits-per-domain.md) | 2026-10-04 | Whether `infrastructure-config` splits into per-domain Kustomizations | **Open** — decide after a real ESO refresh failure or at cluster #2 |

### Open questions without a record yet

Tracked in the relevant `CLAUDE.md` until they're decided:

- Internal-resolver approach for split DNS (0013).
- **Which DNS-zone shape?** ([ADR-0035](0035-site-scope-multiple-proxmox-clusters.md))
  Subdomains of one zone (`<cluster>.example.net`, one Cloudflare token for
  everything) or separate zones (one token each). ⚠ `base_domain` and
  `cloudflare_api_token` are one scope and must fork together — a zone-scoped
  token cannot solve DNS-01 for another zone. Decide with the split-horizon
  resolver question above; they are the same conversation.
- Rendering the vendored CRDs (Calico, Gateway API, ceph-csi) at OCI build
  time instead of vendoring (0020, 0031). Only Calico's is blocked by the
  Ansible prime.
- Control-node kubeconfig hygiene — `ansible/CLAUDE.md`, "Open items".
- Whether ceph-csi-operator moves to its Helm chart now that ADR-0039 removed
  ADR-0031's size blocker. Needs the same server dry-run as evidence first.

## Adding a record

Copy the header shape of any existing file (`Date`, `Status`,
`Supersedes / related`), then `Context` → `Decision` → `Alternatives rejected`
→ `Consequences` → `Evidence`. Number it next in sequence, add a row above, and
— if it reverses an earlier decision — set that one's status to
"Superseded by ADR-NNNN" with a link to the new file. Then update the
reference docs to match and append the worklog.
