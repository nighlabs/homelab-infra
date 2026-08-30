# ADR-0024: Calico eBPF dataplane; k3s's embedded kube-proxy disabled

- **Date:** 2026-08-02 (decided; preconditions verified) · 2026-08-03 (stage 1 and stage 2 done, verified on a from-scratch rebuild)
- **Status:** Accepted
- **Supersedes / related:** [ADR-0010](0010-calico-over-cilium.md) (Calico; its "revisit eBPF only if you drop kube-proxy" note is now exercised), [ADR-0013](0013-ingress-certs-dns-external-access.md) (source-IP preservation was a hard requirement there), [ADR-0018](0018-calico-bgp-replaces-metallb.md) (independent), [ADR-0023](0023-rfc8212-real-policy-le32.md) (why `le 32` still matters), [ADR-0030](0030-flatcar-os-update-policy.md) (kernel drift vs eBPF results). Full record: [`../calico-ebpf-single-node-trial.md`](../calico-ebpf-single-node-trial.md). Code: `gitops/infrastructure/calico/values.yaml` (`linuxDataplane: BPF`), `gitops/infrastructure/calico/kubernetes-services-endpoint.yaml`, `ansible/roles/flatcar_vm/templates/k3s-config.yaml.j2` (`disable-kube-proxy`).

## Context

With the standard iptables/kube-proxy dataplane, external traffic reaching a
Service with `externalTrafficPolicy: Cluster` is SNAT'd by kube-proxy to the
node IP. **The client source IP is destroyed before the pod ever sees the
packet** — unrecoverable at L7. NGINX Gateway Fabric behind a `Cluster`
LoadBalancer would log node IPs as clients, and every downstream access log,
rate limit and IP-based policy would inherit the lie. That is a permanent
design defect, not a tuning problem. The alternative, `Local`, preserves the IP
but confines load balancing to replicas on the ingress node and, with ECMP,
drops traffic that lands on a node with no local pod — the four-row matrix in
the runbook's §10.

Calico's eBPF dataplane **preserves the source IP under `Cluster`** — it
encapsulates the original packet to the backend node rather than rewriting the
source. That collapses the matrix to one row and deletes the `Local`-vs-
`Cluster` trade entirely.

## Decision

**Run Calico's eBPF dataplane (`linuxDataplane: BPF`) and disable k3s's
embedded kube-proxy (`disable-kube-proxy` in the k3s server config).**

- **The reason is source IP, not performance.** At homelab scale the CPU and
  latency savings from dropping kube-proxy are real but negligible; do not let
  them drive the decision.
- **DSR is explicitly NOT part of this.** Source-IP preservation comes from
  eBPF mode itself, in the default `Tunnel` mode. `bpfExternalServiceMode:
  DSR` only optimises the *return* path and in exchange requires the fabric to
  let nodes emit packets sourced from each other's IPs — a real new requirement
  for a rounding-error payoff. It remains a one-line runtime
  `FelixConfiguration` patch if ever wanted.
- **Staged, because the cost asymmetry is large and separable:**

  | Stage | Change | Cost to revert | Buys |
  |---|---|---|---|
  | 1 | `linuxDataplane: BPF` + `bpfKubeProxyIptablesCleanupEnabled: false` (kube-proxy left running) | one `kubectl patch` | **source IP** |
  | 2 | `disable-kube-proxy` in the k3s config | **re-provision** (config comes from Ignition) | CPU/latency only |
  | 3 | second node | — | the tunnel path, ECMP, mixed-mode |

  Stage 1 touched no Ignition and answered the only question that mattered.
  Stopping permanently after stage 1 was a legitimate outcome. Stage 2 was
  then folded into the same rebuild as the version bump
  ([ADR-0019](0019-k3s-1.36-calico-3.32.1-version-pair.md)), collapsing three
  re-provisions into one.
- **Reversibility is what makes this cheap** — and the structural difference
  from an eBPF-only CNI. Calico's dataplane is a switch (`linuxDataplane:
  Iptables` reverts it, documented and supported); policy semantics, CRDs, IPAM
  and BIRD are all unchanged. What does not revert is the debugging toolchain:
  `iptables-save` stops telling you anything and it's `calico-node -bpf`
  instead.
- **Repo integration** — nothing applied by hand once decided:
  `linuxDataplane: BPF` in `values.yaml` (one source, both consumers); the
  `kubernetes-services-endpoint` ConfigMap is **Ansible-primed** (it must exist
  before the dataplane works, so it can't wait for Flux) and carries a node
  IP, so it is topology: committed as `${k3s_api_ip}` and substituted
  ([ADR-0021](0021-topology-blinding-postbuild-substitution.md)) — derived, not
  a new secret. `felixconfiguration.yaml` was **deleted at stage 2**,
  deliberately: it only disabled `bpfKubeProxyIptablesCleanupEnabled` for
  coexistence; with kube-proxy gone, leaving cleanup off would orphan its stale
  iptables rules. Don't reintroduce it without also reverting
  `disable-kube-proxy`.

## Alternatives rejected

- **Stay on iptables and use `externalTrafficPolicy: Local` on the Gateway**
  (the runbook's original advice) — preserves the IP but yields active/standby
  ingress at node 2 (the best-path winner serves everything, the other node's
  replicas sit idle) and blackholes when ECMP lands on a pod-less node.
- **Recover the client IP at L7** — impossible; the component that would add
  the header has already lost the information.
- **DSR** — out, as above.
- **Cilium** — the eBPF-only CNI; already rejected in
  [ADR-0010](0010-calico-over-cilium.md), and its lack of a non-eBPF fallback
  is exactly the reversibility this trial relied on.
- **Do it later, after workloads exist** — the dataplane switch is disruptive
  to existing connections in both directions; harmless now, not later. That
  argued for running the trial immediately.

## Consequences

- **`Cluster` is the default `externalTrafficPolicy`, including on the ingress
  Gateway** — reversing the runbook's old "use `Local` from the start" advice,
  whose sole justification was source-IP preservation. Use `Local` only for a
  Service that genuinely needs traffic pinned to backend-holding nodes — and
  note that changes what is advertised (a /32, not the block), which is the
  case `le 32` exists for ([ADR-0023](0023-rfc8212-real-policy-le32.md)).
- **Preconditions on Flatcar, verified live 2026-08-02 rather than assumed:**
  kernel `6.12.95-flatcar` (needs ≥5.10), `/sys/fs/cgroup` cgroup2 `rw`,
  `/run` a writable tmpfs, bpffs and debugfs already mounted. **⚠ Do NOT copy
  Talos cgroup guidance.** The widely-repeated advice to override
  `CALICO_CGROUP_PATH` / `cgroupV2Path` on "immutable OSes" is
  [calico#7892](https://github.com/projectcalico/calico/issues/7892), which is
  Talos-specific — Talos's rootfs is read-only except `/var`, whereas Flatcar
  makes only `/usr` read-only and `/run` is an ordinary tmpfs. Keep
  `cgroupV2Path` as a **diagnostic** (available at 3.32.1), never a pre-emptive
  setting. General lesson: Talos and Flatcar get lumped together as
  "immutable" and their writable surfaces are nothing alike.
- **Two historical eBPF bugs are already fixed at our pin**, part of why now
  was reasonable: the eBPF-vs-iptables tail-latency regression (eBPF conntrack
  reclaimed faster than the kernel's `TIME_WAIT` → spurious RSTs,
  hundred-ms p99s), fixed in 3.30; and `bpfin.cali`/`bpfout.cali` stuck at MTU
  1500 under a jumbo underlay ([#8868](https://github.com/projectcalico/calico/issues/8868),
  closed by PR #8922) — relevant here because of `mtu 8996` on eth1.
- **Traps that fail confusingly rather than loudly:**

  | Trap | Consequence | Guard |
  |---|---|---|
  | `localhost` in the endpoint ConfigMap | `kube-controllers` dies on `[::1]:6443`, others fine — *partial* failure ([#9141](https://github.com/projectcalico/calico/issues/9141)) | the node's real IP, never a name |
  | Cleanup flag left `true` with kube-proxy running | Felix and kube-proxy overwrite each other's iptables rules on repeat | set with the flip (stage 1 only) |
  | Copying Talos `cgroupV2Path` advice | pointless mount relocation; masks the real cause if something else breaks | verified unnecessary on Flatcar |
  | Reading the result as "eBPF is proven" | the multi-node forwarding path was never exercised | see below |
  | Reverting after workloads exist | connection disruption with real blast radius | it was run early |
  | `internalTrafficPolicy: Local` | ignored in eBPF mode ([#8255](https://github.com/projectcalico/calico/issues/8255)) | not used; don't start |

- **What one node cannot prove — state this whenever the result is cited.**
  The VXLAN tunnel path only runs when the backend is on a *different* node,
  so it was never exercised — and that is exactly where the tail-latency bug
  lived. ECMP / `maximum-paths` / the node-to-node mesh need ≥2 nodes. And
  **mixed eBPF and standard-dataplane nodes are unsupported**, so node 2's
  join is a *cluster-wide* dataplane flip, not a per-node rollout —
  single-node testing structurally hides that.
- **Verifying stage 2 on k3s needs care — the obvious check is worthless.**
  `kubectl -n kube-system get ds kube-proxy` → NotFound proves nothing: k3s
  *embeds* kube-proxy and never had a DaemonSet. And eBPF handles packets
  first, so the source-IP test passes either way. Combined with
  [k3s#9561](https://github.com/k3s-io/k3s/issues/9561) (`disable-kube-proxy`
  once silently ignored from `config.yaml`), it is entirely possible to
  "verify" stage 2 and still be running kube-proxy. What actually proves it:
  no `*proxy*` process in `/proc/*/comm` (not `ps | grep`, which matches
  itself), `:10249` not listening, **0** residual `KUBE-SERVICES` iptables
  chains, and `felix/kube-proxy.go` live in the reconcile loop. **`:10256` IS
  listening and that is correct** — it's Felix's `bpfKubeProxyHealthzPort` at
  its default, deliberately on kube-proxy's port so health checks keep
  working. It is the single most likely observation to make you conclude,
  wrongly, that stage 2 failed.
- **Independent of the BGP work, in both directions.** eBPF replaces
  kube-proxy, not routing — BIRD still carries BGP, LoadBalancer IPs advertise
  identically, and the `frr.conf` is byte-identical either way. The stage-1
  test ran against a **NodePort**, needing no LB IP and no FRR session.
- **eBPF behaviour is kernel-dependent**, and Flatcar auto-updates
  ([ADR-0030](0030-flatcar-os-update-policy.md)): record OS + kernel with any
  result (`4593.2.4` / `6.12.95-flatcar` for this one).
- Server-only k3s flags (`disable-kube-proxy` included) must not reach agent
  join configs.

## Evidence

One falsifiable claim, tested before and after with `externalTrafficPolicy:
Cluster` against a NodePort from an off-cluster machine: before, the pod's
`RemoteAddr` was the **node's** address (SNAT confirmed); after, the **real
off-cluster client**. Stage 1 re-verified on a from-scratch rebuild from
committed config, so the repo owns the result; stage 2 verified by the process
/ port / iptables-chain checks above, with the 0-chain count also confirming
Felix cleaned up after `felixconfiguration.yaml` was deleted. Environment:
Flatcar `4593.2.4` / kernel `6.12.95-flatcar`, k3s `v1.36.2+k3s1`,
`tigera-operator-v3.32.1`, `bpfExternalServiceMode` default (Tunnel). Full
record: [`../calico-ebpf-single-node-trial.md`](../calico-ebpf-single-node-trial.md);
timeline in [`../worklog.md`](../worklog.md).
