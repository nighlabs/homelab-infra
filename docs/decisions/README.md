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
  **Superseded by …**.
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
| [0006](0006-ceph-csi-external-proxmox-ceph.md) | Persistent storage: ceph-csi-operator against the existing Proxmox Ceph | Accepted — not yet implemented |
| [0007](0007-ansible-not-terraform.md) | Provisioning: Ansible only — Terraform/OpenTofu dropped | Accepted |
| [0008](0008-flux-via-flux-operator.md) | GitOps: FluxCD via the Flux Operator, bootstrapped by Ansible last | Accepted (source detail superseded by 0028) |
| [0009](0009-secrets-aescbc-and-eso-bitwarden.md) | Secrets: k3s secrets-encryption at rest; ESO + Bitwarden Secrets Manager for app secrets | Accepted — ESO half not yet implemented |
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
| [0019](0019-k3s-1.36-calico-3.32.1-version-pair.md) | 2026-08-02 | Pin k3s v1.36.x + Calico v3.32.1 as a pair; pre-apply the #12890 RBAC workaround | Accepted, verified |
| [0020](0020-crd-tier-vendored-server-side-apply.md) | 2026-08-02 | A `crds/` tier: Calico's CRDs vendored and server-side applied, `prune: false` | Accepted; build-time render **open** |
| [0021](0021-topology-blinding-postbuild-substitution.md) | 2026-08-02 | Topology as `${var}` placeholders substituted from the `cluster-topology` Secret; SOPS only as fallback | Accepted, verified |
| [0022](0022-pfsense-frr-raw-config-explicit-neighbors.md) | 2026-08-02 | pfSense FRR as generated raw config, explicit `neighbor` statements | Accepted, verified |
| [0023](0023-rfc8212-real-policy-le32.md) | 2026-08-02 | RFC 8212 satisfied by real prefix lists with `le 32`, never disabled | Accepted, verified |
| [0024](0024-calico-ebpf-dataplane-no-kube-proxy.md) | 2026-08-02 | Calico eBPF dataplane; kube-proxy disabled | Accepted, verified |
| [0025](0025-destroy-ignition-snippet-after-first-boot.md) | 2026-08-03 | The Ignition snippet is destroyed after first boot (it embeds the join token) | Accepted, verified |
| [0026](0026-per-cluster-derivation-from-index.md) | 2026-08-16 | Cluster `index` → ASN + LB range; per-cluster token/SANs/version; LB range routed-only | Accepted, verified |
| [0027](0027-control-node-secrets-bws-runtime.md) | 2026-08-17 | Ansible reads BWS at run time; `vault.yml` retired; secret zero in the macOS Keychain | Accepted, live |
| [0028](0028-gitops-delivery-signed-oci-syncless-fluxinstance.md) | 2026-08-29 | Flux consumes a cosign-signed OCI artifact; sync-less FluxInstance; self-managed root | Accepted, verified |
| [0029](0029-drop-helm-for-calico.md) | 2026-08-02 | Install the tigera operator from manifests instead of the Helm chart | **Proposed** |
| [0030](0030-flatcar-os-update-policy.md) | 2026-08-02 | Flatcar auto-update/reboot policy | **Open** |

### Open questions without a record yet

Tracked in the relevant `CLAUDE.md` until they're decided:

- Internal-resolver approach for split DNS (0013).
- Rendering Calico's CRDs at OCI build time instead of vendoring (0020).
- Control-node kubeconfig hygiene — `ansible/CLAUDE.md`, "Open items".
- Whether ESO, once live, **adopts** the cluster-destined bootstrap-seeded
  Secrets (first case: cert-manager's Cloudflare DNS-01 token), or they stay
  Ansible-seed-only (0009, 0027). **Not yet decided** — the seed-only wording
  in 0009 records the mechanism, not a permanence decision. Current lean:
  adopt, inside 0027's consumer split — ESO's machine account still never
  reads `homelab-infra` (cluster-destined secrets would live in the apps
  project, with the *control-node* account granted read on both; exposure
  doesn't widen because these secrets end up as in-cluster `Secret`s either
  way), and **the Ansible seed remains regardless** — a from-scratch rebuild
  needs the token before ESO exists, so adoption is an overlay, never a
  handover. Excluded whatever is decided: `cluster-topology` (permanent,
  0021/0027) and `eso_bws_access_token` (0027 ⚠). Decide + ADR at the ESO
  milestone.

## Adding a record

Copy the header shape of any existing file (`Date`, `Status`,
`Supersedes / related`), then `Context` → `Decision` → `Alternatives rejected`
→ `Consequences` → `Evidence`. Number it next in sequence, add a row above, and
— if it reverses an earlier decision — set that one's status to
"Superseded by ADR-NNNN" with a link to the new file. Then update the
reference docs to match and append the worklog.
