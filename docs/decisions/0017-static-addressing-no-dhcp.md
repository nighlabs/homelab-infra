# ADR-0017: All node addressing is static, rendered into Ignition from the node map; no DHCP

- **Date:** 2026-07-07 (decided and verified on `snoop-a2o`)
- **Status:** Accepted
- **Supersedes / related:** reverses the original design's DHCP assumption in [`../architecture.md`](../architecture.md) §3.3; [ADR-0005](0005-flatcar-k3s-sysext-ignition-config-drive.md) (Ignition delivery); [ADR-0026](0026-per-cluster-derivation-from-index.md) (the same one-number-derives-everything idea, at cluster scope). Code: `ansible/inventory/nodes.yml`, `ansible/roles/flatcar_vm/tasks/preflight.yml`, `ansible/roles/flatcar_vm/templates/*.network.j2`, `ansible/playbooks/tasks/load-node-map.yml`.

## Context

The original design doc said "DHCP sidesteps the fragile config-drive
network-data path" — i.e. let the node get its address from DHCP rather than
lean on cloud-init's `network-data` delivery. That warning was about a specific
mechanism (cloud-init's network-data channel), not about static addressing in
general. Meanwhile the cluster's networking has properties DHCP serves badly:
two NICs per node, one of them on a tagged jumbo-frame VLAN with no gateway,
where getting the wrong VLAN silently lands traffic on Ceph's replication
network; a BGP peering whose `neighbor` lines are fixed addresses; and a
"delete the VM and re-run the play" rebuild model that should reproduce the
same identity every time.

## Decision

**Addressing is static, defined directly in each node's Ignition, sourced from
the Ansible node map. There is no DHCP anywhere in cluster networking.** This
*reverses* the design doc's DHCP line — and it is a different mechanism than
the one that warning was about: defining static addresses in Ignition's own
`systemd-networkd` units sidesteps the cloud-init network-data path entirely
rather than triggering it.

- **Source of truth: `inventory/nodes.yml`.** Nodes are grouped under the k3s
  cluster they belong to; only `node_number` is required per node, and
  everything host-shaped derives from it: DMZ IP `<dmz_subnet_base>.<n>`,
  Ceph-public IP `<ceph_subnet_base>.<n>`, MACs `<mac_oui>:00:<n hex>:0{0,1}`,
  `vmid` `1000+n`. The same map drives VM creation, so network identity lives
  right alongside CPU/RAM/role — one place to look, one place to change.
- **MAC addresses are pinned at VM-creation time** via `proxmox_kvm`'s
  `net0`/`net1` `macaddr`, from the same node map. This makes Ignition's
  `[Match] MACAddress=` stanza fully deterministic: it no longer matters whether
  Proxmox/virtio names the interface `eth0`, `enp6s0`, or anything else — the
  network unit finds the NIC by MAC regardless of naming, ordering, or image
  quirks.
- **Butane network units are rendered per node with Jinja2** from the node
  map (`00-eth0.network.j2`, `10-eth1.network.j2`).
- **`eth1` gets no `Gateway=`** — it must only reach hosts on the Ceph public
  network, never route anywhere else. `MTUBytes=8996` is set explicitly in the
  unit *and* on the VM's `net1` hardware definition; neither side inherits the
  bridge's jumbo setting.
- **DNS servers and hostname are set explicitly** — with DHCP gone nothing
  hands the VM a resolver or option 12. DNS comes from a `dns_servers` list in
  group vars; the hostname is a `storage.files` entry for `/etc/hostname`
  rendered from the node map.
- **Uniqueness is asserted, globally.** `playbooks/tasks/load-node-map.yml`
  flattens `clusters` → a cluster-annotated `nodes` map and asserts
  hostname/`node_number` uniqueness across *every* cluster — all clusters share
  the DMZ/Ceph subnets and the Proxmox vmid space, so the check can't be
  per-cluster.

## Alternatives rejected

- **DHCP with reservations** (the design doc's original position) — rejected:
  a lease table and reservation set outside Git that must be kept in sync with
  the node map; a second NIC with no gateway and a mandatory VLAN tag is not
  something a DHCP reservation expresses; and a rebuild would depend on an
  external server handing back the same identity.
- **cloud-init `network-data` via the config drive** — the "fragile path" the
  design doc warned about; Flatcar can't consume Ignition and cloud-init on the
  same channel anyway (see [ADR-0005](0005-flatcar-k3s-sysext-ignition-config-drive.md)).
- **Matching interfaces by name** (`eth0`/`eth1`) instead of MAC — rejected:
  interface naming under virtio is not guaranteed stable across images or
  hardware order, and a mismatch leaves the NIC unconfigured with no error.

## Consequences

- **Repo-wide guardrail:** no DHCP anywhere in cluster networking. Recorded in
  the root `CLAUDE.md`.
- **Dropping DHCP also drops its free IP-collision protection.** Nothing
  outside the node map stops the same address being handed to two nodes if the
  map has a typo or a stale entry, and there is no lease table to cross-check.
  The `load-node-map.yml` uniqueness assert is what replaces it — it runs
  before anything is created.
- Node addresses are deterministic from `node_number`, which is what lets the
  pfSense BGP config carry explicit `neighbor` lines that stay valid across a
  destroy-and-recreate ([ADR-0022](0022-pfsense-frr-raw-config-explicit-neighbors.md)).
- Isolation on `eth1` comes from the VLAN itself, not from per-host firewall
  rules — deliberately, so it scales as the Ceph cluster grows. Never leave
  `eth1` untagged on its bridge: untagged lands on Ceph's cluster/replication
  network.
- Full rebuildability: delete the VM, re-run the play, and the same MAC +
  static IP + hostname come back with no DHCP server, lease, or reservation to
  keep in sync anywhere outside Git.

## Evidence

Verified on `snoop-a2o` 2026-07-07 against the full definition of done: static
addresses on both NICs, MACs matching the pinned values (so `[Match]
MACAddress=` is provably doing the binding), `eth1` at MTU 8996 with no default
route, DNS via the static resolvers, hostname from the map, key-only SSH,
unattended reboot, and an identical result from a from-scratch rebuild. See
[`../worklog.md`](../worklog.md) and `ansible/README.md` → "Verify".
