# ADR-0013: Edge: NGINX Gateway Fabric, cert-manager DNS-01 wildcard, split-horizon DNS, Cloudflare Tunnel + Tailscale, source-IP preservation

- **Date:** 2026-07 (initial design)
- **Status:** Accepted — ingress half implemented (NGF + shared Gateway, 2026-09-05, see worklog); certs, DNS and external access not yet; the internal-resolver approach is still **Open**
- **Supersedes / related:** [ADR-0012](0012-metallb-bgp.md) → [ADR-0018](0018-calico-bgp-replaces-metallb.md) (where the Gateway's LoadBalancer IP comes from); [ADR-0011](0011-cluster-cidrs-never-cgnat.md); [ADR-0024](0024-calico-ebpf-dataplane-no-kube-proxy.md) (changes the source-IP story); `../architecture.md` §4.5–§4.10

## Context

Services in the cluster need TLS, routing, a name, and a way to be reached
from three places: the LAN, the tailnet, and the public internet — with two
hard requirements: **no inbound port-forwarding**, and **the real client IP
must reach the workload**. Local access must not become WAN-dependent.

## Decision

**Ingress — Gateway API via NGINX Gateway Fabric (NGF).** Future-proof over
legacy Ingress; cert-manager and external-dns both speak Gateway API. k3s's
bundled Traefik and servicelb are disabled in favour of NGF + a LoadBalancer
IP — originally from MetalLB ([ADR-0012](0012-metallb-bgp.md)), now
**allocated and advertised by Calico** ([ADR-0018](0018-calico-bgp-replaces-metallb.md)).

**Certificates — cert-manager + ACME via Cloudflare DNS-01.** DNS-01, not
HTTP-01: valid public certs with **no inbound**, so even internal-only services
get real certs. Issue a **wildcard** so the same cert serves the internal
endpoint too.

**Split-horizon DNS.**
- *Public view:* Cloudflare Tunnel (external-dns) for off-tailnet remote
  clients.
- *Internal view:* an internal resolver returns the Gateway's LoadBalancer IP
  for the same hostnames, so LAN traffic stays local — no WAN, no NAT
  reflection.
- *Tailnet view:* Tailscale split DNS (per-domain nameserver) points the zone
  at the internal resolver.
- Certs just work: the DNS-01 wildcard means the internal endpoint serves the
  same valid cert — no TLS mismatch, no internal CA.
- **pfSense gotcha:** Unbound's DNS-rebinding protection blocks private
  answers for public domains — whitelist the domain, or the internal overrides
  get stripped.
- **Open — pick one:** pfSense Unbound host overrides (simplest); a single
  internal wildcard `*.apps.<domain>` → Gateway IP with HTTPRoute host routing
  (lowest maintenance); or a second external-dns instance with an internal
  provider (most GitOps-native). Test split-DNS behaviour, including the
  rebinding whitelist, before relying on the internal view.

**External access — Cloudflare Tunnel (public) + Tailscale (private).**
Cloudflare Tunnel for public exposure with zero port-forwarding (outbound-only
→ internal Gateway), with Cloudflare Access for auth on sensitive routes.
Tailscale is the **human/remote-access layer** (your devices → the Mac and the
cluster), not an inter-tier data path, and the candidate to replace the
separate WireGuard setup.

**Source-IP preservation (hard requirement).**
- *Internal / direct path:* the design originally required
  `externalTrafficPolicy: Local` on the Gateway's LoadBalancer Service, because
  kube-proxy SNATs `Cluster`-policy traffic to the node IP. **That changed
  with the eBPF dataplane** ([ADR-0024](0024-calico-ebpf-dataplane-no-kube-proxy.md)):
  Calico eBPF preserves the source IP under `Cluster`, so `Cluster` is now the
  default and `Local` is reserved for Services that genuinely need traffic
  pinned to backend-holding nodes.
- *Cloudflare path:* L4 preservation is **impossible** — cloudflared forwards
  to the Gateway via ClusterIP, so cloudflared is the origin peer. Recover the
  real client IP from `CF-Connecting-IP` / `X-Forwarded-For` via NGF's
  `NginxProxy` **RewriteClientIP** (`mode: XForwardedFor`, `trustedAddresses`
  = the cloudflared source, `setIPRecursively: true`), trusting **only**
  cloudflared.
- *Tailscale wrinkle:* subnet routers SNAT by default — to preserve the real
  tailnet client IP set `--snat-subnet-routes=false` plus a return route to
  `100.64.0.0/10`.

**Inter-tier hop — LiteLLM → Mac stays on the LAN, not Tailscale.** The two
tiers are one switch-hop apart; LiteLLM reaches the Mac at a stable LAN name.
The Mac sits on its own VLAN with a firewall rule allowing only the cluster to
reach `:8080`. **Do not** expose llama-swap publicly; public exposure, if any,
is LiteLLM or Open WebUI behind TLS + auth. LiteLLM holds the real auth
boundary — per-client/per-agent virtual keys with budgets.

## Alternatives rejected

- **Legacy Ingress resources.** Gateway API is the forward path and the
  supporting tooling already speaks it.
- **HTTP-01 challenges.** Require inbound reachability, contradicting the
  no-port-forward goal.
- **Routing *all* local traffic through Cloudflare Tunnel.** Makes LAN access
  WAN-dependent (breaks local-first) and causes NAT reflection / hairpinning
  — the prior "NAT oddities." Split DNS avoids NAT entirely.
- **An internal CA for internal endpoints.** Unnecessary once the DNS-01
  wildcard serves both views.
- **Tailscale as the inter-tier data path.** An overlay dependency on a
  one-hop LAN path; if the Mac ever leaves the LAN, repointing `api_base` at a
  tailnet name is a one-line change bought later.

## Consequences

- Split DNS only preserves **local** access during a WAN outage; remote access
  during *your own* WAN outage is unsolvable.
- The Gateway's LoadBalancer IP is the thing the whole ESO dependency chain
  waits on (Gateway → cert-manager cert → Bitwarden SDK Server → ESO), which is
  why BGP is bootstrap-tier ([ADR-0009](0009-secrets-aescbc-and-eso-bitwarden.md),
  [ADR-0018](0018-calico-bgp-replaces-metallb.md)).
- Traffic to the LoadBalancer range from other segments needs its own
  firewall pass rules; the LB supernet belongs in the "internal networks"
  alias so reachability fails closed (`../pfsense-frr-bgp-setup.md` §6).
- Tailscale's presence is what makes CGNAT unusable for cluster CIDRs
  ([ADR-0011](0011-cluster-cidrs-never-cgnat.md)).
- Verify source-IP preservation on **both** paths (direct and Cloudflare) at
  bring-up; they fail differently.
