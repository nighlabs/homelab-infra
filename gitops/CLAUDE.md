# CLAUDE.md — gitops/ (Flux-managed cluster contents)

**Not populated yet** — this directory is empty until Ansible bootstraps
Flux (see `ansible/CLAUDE.md` §6, step 4). When that happens, replace this
placeholder with:

- The dependency order workloads land in (Calico → MetalLB → NGINX Gateway
  Fabric + cert-manager → ceph-csi-operator → ESO + Bitwarden SDK Server →
  Postgres/Redis → LiteLLM → Qdrant → RAG/orchestrator → Open WebUI → OTel
  Collector), matching `ansible/CLAUDE.md` §6.
- Repo conventions once established: how `clusters/`, `infrastructure/`, and
  `apps/` are organized; `HelmRelease`/`Kustomization` naming; `dependsOn`
  patterns in use.
- Any unknowns specific to GitOps delivery (e.g. Flux reconciliation
  ordering surprises, secrets sync timing) as they're discovered.

Standing repo-wide guardrails (no Terraform, no second Ceph, no CGNAT CIDRs,
no DHCP) still apply here — see the root `CLAUDE.md`; don't duplicate them
in this file once it's filled in.
