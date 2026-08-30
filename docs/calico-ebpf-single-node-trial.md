# Calico eBPF dataplane — single-node trial on `snoop-a2o`

> **Status: ✅ RUN AND PASSED, 2026-08-03 — BOTH STAGES COMPLETE AND COMMITTED.**
> Kept as the reasoning record and the revert procedure; it is no longer a plan.
> Decision: [ADR-0024](decisions/0024-calico-ebpf-dataplane-no-kube-proxy.md).
> Nothing here was a prerequisite for the Calico BGP work — the two are
> independent (see §8), and that held.
>
> **Result — `externalTrafficPolicy: Cluster` throughout, three requests each:**
>
> | | Pod's observed `RemoteAddr` |
> |---|---|
> | Before (iptables/kube-proxy) | the **node's** address — SNAT |
> | After (eBPF, Tunnel mode) | the **real off-cluster client** ✅ |
>
> Environment: Flatcar `4593.2.4` / kernel `6.12.95-flatcar`, k3s `v1.36.2+k3s1`,
> `tigera-operator-v3.32.1`, `bpfExternalServiceMode` left at default (Tunnel,
> `DSR:false`). **Stage 1 re-verified on a from-scratch rebuild from committed
> config**, so the result belongs to the repo rather than to hand-applied
> patches. **Stage 2** then removed kube-proxy entirely (`disable-kube-proxy` in
> the k3s config) — see §7 for what "verified" had to mean there, since the
> obvious check is worthless on k3s.
>
> **⚠ §8 still applies in full** — one node cannot exercise the VXLAN tunnel
> path, ECMP, or mixed-mode. Cite the result with that attached.

**Blinding rule (same as every committed doc):** no real addresses, ASNs, or
hostnames. `${placeholder}` only. The one literal here is the pod CIDR
`10.42.0.0/16`, which is already public in the root `CLAUDE.md`.

---

## 1. What this buys, and why it's worth a trial

Today, external traffic reaching a Service with `externalTrafficPolicy: Cluster`
is SNAT'd by kube-proxy to the node IP. Two consequences:

- **The client source IP is destroyed** before the pod ever sees the packet.
  Unrecoverable at L7 — NGINX Gateway Fabric behind a `Cluster` LoadBalancer
  would log node IPs as clients, and every downstream access log, rate limit,
  and IP-based policy inherits the lie. This is a permanent design defect, not
  a tuning problem.
- **It costs a conntrack entry and a NAT hop per flow**, plus (on multi-node) a
  forced hairpin back through the ingress node.

Calico's eBPF dataplane **preserves the source IP with `externalTrafficPolicy:
Cluster`** — it encapsulates the original packet to the backend node rather
than rewriting the source. That collapses the four-row matrix in
`pfsense-frr-bgp-setup.md` §10 to a single row:

| | Node-level LB | Pod-level LB | Source IP |
|---|---|---|---|
| eBPF + `Cluster` + ECMP | ✓ per-flow across nodes | ✓ all replicas cluster-wide | **✓ preserved** |

The `Local`-vs-`Cluster` trade stops existing. **That — not throughput — is the
reason to do this.** At homelab scale the CPU and latency savings from dropping
kube-proxy are real but negligible; do not let them drive the decision.

**⚠ DSR is explicitly NOT part of this.** Source IP preservation comes from
eBPF mode itself, in the default `Tunnel` mode. `bpfExternalServiceMode: DSR`
only optimises the *return* path (reply leaves from the backend node instead of
hairpinning) and in exchange requires the fabric to let nodes emit packets
sourced from each other's IPs. On one subnet with a handful of services that is
a rounding error for a real new requirement. Leave it at `Tunnel`.

---

## 2. Preconditions — verified live on `snoop-a2o`, 2026-08-02

Checked on the running node, not inferred:

| Requirement | Needed | Found | |
|---|---|---|---|
| Kernel | ≥ 5.10 | `6.12.95-flatcar` | ✓ |
| OS | — | Flatcar 4593.2.4 (Oklo) | ✓ |
| cgroup v2 unified, writable | for CTLB attach | `cgroup2 /sys/fs/cgroup cgroup2 rw,…` | ✓ |
| `/run` writable | for `/run/calico/cgroup` | `tmpfs /run tmpfs rw,…`; `mkdir` OK | ✓ |
| bpffs mounted | `mount-bpffs` init container | `bpf /sys/fs/bpf bpf rw,…` | ✓ |
| debugfs mounted | eBPF tooling | `debugfs /sys/kernel/debug debugfs rw,…` | ✓ |

**⚠ Do not apply Talos guidance to Flatcar.** There is widely-repeated advice
that immutable distros need `CALICO_CGROUP_PATH` / `FelixConfiguration.
cgroupV2Path` overridden because `/run/calico/cgroup` is read-only. That is
[calico#7892](https://github.com/projectcalico/calico/issues/7892), and it is
**Talos-specific** — Talos's rootfs is read-only "with the exception of specific
files and the entirety of `/var`". Flatcar's immutability is a different shape:
only `/usr` is read-only (separate partition, dm-verity, USR-A/USR-B), while
`/run` is an ordinary systemd tmpfs. The table above confirms it empirically.

Keep `cgroupV2Path` as a **diagnostic, not a prerequisite** — available on our
3.32.1 pin (landed in v3.31 via
[PR #8085](https://github.com/projectcalico/calico/pull/8085)) if a future
Flatcar release changes the layout. Setting it pre-emptively to
`/sys/fs/cgroup` just moves Calico's mount into the host's live cgroup root for
no gain.

Two historical eBPF bugs are **already fixed at our pin**, which is part of why
now is a reasonable time to try:

- eBPF-vs-iptables tail-latency regression (eBPF conntrack entries reclaimed
  faster than the kernel's `TIME_WAIT`, producing spurious RSTs and
  hundred-millisecond p99s) — fixed in **3.30** via configurable conntrack
  timeouts.
- `bpfin.cali`/`bpfout.cali` pinned at MTU 1500 under a jumbo underlay
  ([#8868](https://github.com/projectcalico/calico/issues/8868)) — closed by
  PR #8922. Relevant here specifically because of jumbo (`mtu 8996`) on eth1.

---

## 3. The staging model — the whole point of the plan

The cost asymmetry between "turn on eBPF" and "remove kube-proxy" is large, and
they are separable. **Source IP preservation does not require kube-proxy to be
gone.** Calico documents coexistence: leave kube-proxy running and stop Felix
from cleaning up its rules.

| Stage | Change | Cost to revert | Buys |
|---|---|---|---|
| **1** | `linuxDataplane: BPF` + `bpfKubeProxyIptablesCleanupEnabled: false` | one `kubectl patch` | **source IP** |
| **2** | `--disable-kube-proxy` in the k3s config | **re-provision** (config comes from Ignition) | CPU/latency only |
| **3** | second node | — | the tunnel path, ECMP, mixed-mode |

Stage 1 touches no Ignition, needs no re-provision, and answers the only
question that actually matters. **Stopping permanently after stage 1 is a
legitimate outcome** — stage 2 buys efficiency we don't need.

**⚠ Coexistence is documented-but-not-preferred.** If stage 1 behaves oddly,
kube-proxy coexistence is the first suspect and stage 2 is the *resolution*, not
a regression. Budget for that before concluding eBPF is at fault.

---

## 4. Stage 1 — flip the dataplane

### 4a. The `kubernetes-services-endpoint` ConfigMap

In eBPF mode Calico implements Service networking itself, so it needs to reach
the API server **without** going through a ClusterIP. This ConfigMap is how it
finds it, and it must exist *before* the dataplane flip.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: kubernetes-services-endpoint
  namespace: tigera-operator
data:
  KUBERNETES_SERVICE_HOST: "${k3s_api_ip}"
  KUBERNETES_SERVICE_PORT: "6443"
```

**⚠ Use the node's real IP, never `localhost`.**
[calico#9141](https://github.com/projectcalico/calico/issues/9141) is exactly
this mistake: `calico-kube-controllers` resolved `localhost` to IPv6 and died
with `dial tcp [::1]:6443: connect: connection refused`, while `calico-node` and
`typha` came up fine. It fails *partially*, which is far worse than failing
outright.

`${k3s_api_ip}` is **derived, not a new vault variable** — it's
`{{ dmz_network.subnet_base }}.{{ node_number }}` from `inventory/nodes.yml`
(node_number 50 for `snoop-a2o`), exactly like every other node address. Ansible
templates it; the committed manifest carries the placeholder.

After applying, allow ~60s for pickup, then restart the operator:

```bash
kubectl delete pod -n tigera-operator -l k8s-app=tigera-operator
```

### 4b. The dataplane flip

```bash
kubectl patch installation.operator.tigera.io default --type merge \
  -p '{"spec":{"calicoNetwork":{"linuxDataplane":"BPF"}}}'
```

### 4c. Keep Felix and kube-proxy from fighting

Because kube-proxy is still running in stage 1:

```bash
kubectl patch felixconfiguration default --type merge \
  -p '{"spec":{"bpfKubeProxyIptablesCleanupEnabled":false}}'
```

Without this, kube-proxy writes its iptables rules and Felix deletes them, on
repeat — iptables flapping, and a genuinely confusing failure. Set it **before**
or immediately with the flip, not after you notice.

Leave `bpfExternalServiceMode` at its default `Tunnel` (§1).

---

## 5. The test — one falsifiable claim

**Claim:** with `externalTrafficPolicy: Cluster`, the pod sees the real client
IP after the flip and does not before it.

**This needs no LoadBalancer IP and no BGP session** — source IP preservation is
documented for the NodePort path, so the test runs against a NodePort today,
independent of the pfSense work.

```bash
kubectl create deploy echo --image=ealen/echo-server --port=80
kubectl expose deploy echo --type=NodePort --port=80 \
  --overrides='{"spec":{"externalTrafficPolicy":"Cluster"}}'
kubectl get svc echo -o jsonpath='{.spec.ports[0].nodePort}{"\n"}'
```

From a machine **off-cluster** (a laptop on the DMZ — not the node itself, or
the test is meaningless):

```bash
curl -s http://${k3s_api_ip}:<nodePort>/ | jq -r '.request.headers'
kubectl logs deploy/echo | tail -5
```

| | Expected |
|---|---|
| **Before the flip** | source shows the **node's** IP — SNAT confirmed |
| **After the flip** | source shows the **laptop's** IP — ✅ PASS |

Record both outputs. "It seemed to work" is not a result; the before-value is
what makes the after-value mean anything.

Also confirm the dataplane is genuinely active rather than silently inert:

```bash
kubectl exec -n calico-system ds/calico-node -- calico-node -bpf conntrack dump | head
kubectl logs -n calico-system ds/calico-node | grep -i "bpf.*enabled"
```

---

## 6. Revert

Reverting is documented and supported — this is the property that makes the
trial cheap, and the main structural difference from an eBPF-only CNI:

```bash
kubectl patch installation.operator.tigera.io default --type merge \
  -p '{"spec":{"calicoNetwork":{"linuxDataplane":"Iptables"}}}'
kubectl patch felixconfiguration default --type merge \
  -p '{"spec":{"bpfEnabled":false}}'
```

**⚠ The switch is disruptive to existing connections in both directions.**
Harmless today (no workloads, no PVCs); not harmless later. This is another
argument for running the trial now rather than after there's state.

If stage 2 was taken, revert additionally requires putting `--disable-kube-proxy`
back and re-provisioning — which is precisely why stage 2 is separate.

---

## 7. Stage 2 — remove kube-proxy ✅ DONE 2026-08-03

**⚠ The obvious verification is worthless here.** `kubectl -n kube-system get ds
kube-proxy` returning NotFound proves nothing on k3s — **k3s embeds kube-proxy
and never had a DaemonSet**. Combined with
[k3s#9561](https://github.com/k3s-io/k3s/issues/9561) (`disable-kube-proxy`
silently ignored from `config.yaml` while working as a CLI flag — fixed long
before 1.36, but silent when it bites), it is entirely possible to "verify"
stage 2, still be running kube-proxy, and still pass the source-IP test because
eBPF handles the packet first.

What actually proved it, on the node:

| Check | Result |
|---|---|
| process named `*proxy*` in `/proc/*/comm` | none |
| `:10249` — kube-proxy's metrics port | not listening |
| residual `KUBE-SERVICES` iptables chains | **0** |
| `felix/kube-proxy.go` + `resync-kube-proxy-v4` in the reconcile loop | present — Calico's implementation is the live one |

**⚠ `:10256` IS listening, and that is correct.** It's kube-proxy's healthz port,
but the owner is Felix's `bpfKubeProxyHealthzPort` at its default — Calico
deliberately serves on the standard port so health checks expecting kube-proxy
keep working. This is the single most likely observation to make you conclude,
wrongly, that stage 2 failed.

Use `/proc/*/comm` rather than `ps | grep` — process *names* can't self-match,
whereas a grep pattern containing `kube-proxy` matches the shell running it.
(That produced a false positive first time through.)

The zero residual chains also confirm deleting `felixconfiguration.yaml` worked
as intended: `bpfKubeProxyIptablesCleanupEnabled` returned to its default and
Felix removed kube-proxy's stale rules instead of orphaning them.

Steps taken, for the record — only after stage 1 passed:

1. Drop `--disable-kube-proxy` into the k3s server config
   (`roles/*/templates/k3s-config.yaml.j2` — a server-only flag, per §8's
   guardrail it must not reach agent joins).
2. Re-provision. Per [ADR-0019](decisions/0019-k3s-1.36-calico-3.32.1-version-pair.md),
   re-provision beats in-place while the cluster is disposable.
3. Set `bpfKubeProxyIptablesCleanupEnabled` back to `true` (its default) —
   there's nothing left to coexist with, and leaving cleanup disabled orphans
   kube-proxy's old rules.

Fold this into the same rebuild as the k3s 1.36 / Calico 3.32.1 bump if it's
going to happen at all — three re-provisions collapse into one.

---

## 8. What one node cannot prove

State this plainly whenever the trial's result is cited:

- **The VXLAN tunnel path never runs.** In `Tunnel` mode the encapsulation only
  happens when the backend pod is on a *different* node. With one node it is
  never exercised — and that is exactly where the tail-latency bug lived.
- **ECMP / `maximum-paths` / the node-to-node mesh** need ≥2 nodes.
- **⚠ Mixed eBPF and standard-dataplane nodes are unsupported.** Adding node 2
  is therefore a *cluster-wide* flip, not a per-node rollout. Single-node
  testing structurally hides this. Plan node 2's join as a dataplane event.

**Independence from the BGP work.** eBPF replaces kube-proxy, not routing —
BIRD still carries BGP, LoadBalancer IPs still advertise identically. The
`frr.conf` in `pfsense-frr-bgp-setup.md` (peer group, `<CLUSTER>-IN`/`-OUT`,
`maximum-paths 8`) is byte-identical either way. Neither task blocks the other,
in either order.

> **⚠ One consequence for the BGP side, added 2026-08-16.** eBPF preserving the
> source IP under `externalTrafficPolicy: Cluster` is what makes `Cluster` the
> default choice — and `Cluster` advertises the **whole LB block**, where `Local`
> advertises a **/32 per Service**. The pfSense prefix list therefore needs
> `le 32` to accept both; without it a `Local` Service establishes a healthy
> session and silently blackholes. See `pfsense-frr-bgp-setup.md` §4. This
> supersedes that runbook's old "use `Local` on the ingress Gateway" advice,
> whose sole justification was source-IP preservation.

---

## 9. Repo integration

Nothing here is applied by hand once it's decided — same "Ansible primes, Flux
adopts" pattern as everything else (`gitops/CLAUDE.md`):

- **`gitops/infrastructure/calico/values.yaml`** grows
  `installation.calicoNetwork.linuxDataplane: BPF`, alongside the `bgp: Enabled`
  + no-encapsulation change already queued. It stays the single values source
  feeding both the Ansible prime and the Flux `configMapGenerator`, so the two
  can't drift.
- **The `kubernetes-services-endpoint` ConfigMap is Ansible-primed**, like
  `BGPPeer` and the #12890 RBAC workaround — it must exist before the dataplane
  works, so it cannot wait for Flux.
- It carries a node IP, so it is **topology**: committed with `${k3s_api_ip}`
  and resolved via `postBuild.substituteFrom` the `cluster-topology` Secret, or
  templated by Ansible at prime time. Never a literal in Git.
- `FelixConfiguration` tweaks (`bpfKubeProxyIptablesCleanupEnabled`) are CRs,
  not Helm values — plain manifests in `infrastructure/calico/`, same as the BGP
  CRs.

---

## 10. Known traps, collected

| Trap | Consequence | Guard |
|---|---|---|
| `localhost` in the endpoint ConfigMap | `kube-controllers` dies on `[::1]:6443`, others fine — *partial* failure | real IP, never a name (#9141) |
| Cleanup flag left `true` with kube-proxy running | iptables flapping between Felix and kube-proxy | §4c, set with the flip |
| Copying Talos `cgroupV2Path` advice | pointless mount relocation; masks the real cause if something else breaks | §2 — verified unnecessary on Flatcar |
| Reading the result as "eBPF is proven" | the multi-node forwarding path was never exercised | §8 |
| Reverting after workloads exist | connection disruption with real blast radius | run the trial now |
| `internalTrafficPolicy: Local` | ignored in eBPF mode ([#8255](https://github.com/projectcalico/calico/issues/8255)) | not used here; don't start |

---

## Sources

- [About Calico eBPF](https://docs.tigera.io/calico/latest/about/kubernetes-training/about-ebpf) — source IP preservation, DSR support matrix
- [Enabling the eBPF data plane](https://docs.tigera.io/calico/latest/operations/ebpf/enabling-ebpf) — `linuxDataplane`, endpoint ConfigMap, revert procedure
- [Felix configuration reference](https://docs.tigera.io/calico/latest/reference/felix/configuration) — `BPFExternalServiceMode` (default `Tunnel`), `BPFDSROptoutCIDRs`
- [Source IP preservation & tail latency](https://www.tigera.io/blog/calico-ebpf-source-ip-preservation-the-unexpected-story-of-high-tail-latency/) — the conntrack-timeout fix in 3.30
- [calico#7892](https://github.com/projectcalico/calico/issues/7892) — Talos cgroup2 mount failure (does **not** apply to Flatcar)
- [calico#9141](https://github.com/projectcalico/calico/issues/9141) — `kube-controllers` vs `localhost:6443`
- [calico#8868](https://github.com/projectcalico/calico/issues/8868) — bpf interface MTU under a jumbo underlay
- [k3s server CLI](https://docs.k3s.io/cli/server) — `--disable-kube-proxy`
