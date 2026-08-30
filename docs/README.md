# Documentation map

Three kinds of document, kept deliberately separate. When adding to the docs,
put each piece of content where its *kind* belongs — mixing them is how the
reference docs turned into a worklog.

| Kind | Answers | Lives in | Tense / style |
|---|---|---|---|
| **Reference** | *How does it work now?* | [`architecture.md`](architecture.md) · the runbooks · each directory's `README.md` and `CLAUDE.md` | Present tense. No dates, no strikethroughs, no "SUPERSEDED". When something changes, the text is rewritten, and the old text moves to the worklog or a decision record. |
| **Decisions** | *Why is it this way, and what was rejected?* | [`decisions/`](decisions/README.md) — one file per decision, numbered, with status | Context → decision → alternatives rejected → consequences. Never edited to change the past: a reversal is a **new** ADR that supersedes the old one, and the old one's status line says so. |
| **Worklog** | *What happened when, and what proved it?* | [`worklog.md`](worklog.md) — newest first | Append-only. Evidence tables, failures found, lessons. Later entries can contradict earlier ones; earlier ones are not rewritten. |

## Where to start

- **New here?** [`../README.md`](../README.md) for the one-screen overview and
  current status, then [`architecture.md`](architecture.md).
- **Running it?** [`../ansible/README.md`](../ansible/README.md) — prerequisites,
  one-time Proxmox/BWS setup, the plays, the definition-of-done checks, and
  troubleshooting. The secrets manifest is
  [`../ansible/BWS-SECRETS.md`](../ansible/BWS-SECRETS.md).
- **Changing cluster contents?** [`../gitops/CLAUDE.md`](../gitops/CLAUDE.md) —
  the four-tier layout, the adoption pattern, the substitution rules and their
  traps.
- **Touching the plays or roles?** [`../ansible/CLAUDE.md`](../ansible/CLAUDE.md)
  — current state, what's next, and the non-obvious facts about Flatcar,
  Ignition, k3s and Ansible-on-this-repo that will otherwise be re-learned.
- **A choice looks arbitrary?** It almost certainly isn't —
  [`decisions/README.md`](decisions/README.md) lists every decision with its
  status. Read the ADR before re-litigating.
- **pfSense / BGP:** [`pfsense-frr-bgp-setup.md`](pfsense-frr-bgp-setup.md) —
  the runbook for the FRR side of the peering.
- **The eBPF trial:** [`calico-ebpf-single-node-trial.md`](calico-ebpf-single-node-trial.md)
  — the full record of the dataplane migration: preconditions, the test, the
  revert, and what one node cannot prove.

## Files

```
docs/
  README.md                          this map
  architecture.md                    the design + what's built (reference)
  decisions/                         ADRs — the decision log
    README.md                          index with status
    NNNN-slug.md                       one per decision
  worklog.md                         chronological record, newest first
  pfsense-frr-bgp-setup.md           runbook: FRR/BGP on pfSense (reference)
  calico-ebpf-single-node-trial.md   record of the eBPF dataplane trial
```

## Conventions

- **Blinding.** The repo and its OCI artifact are public. No real subnets, VLAN
  tags, bridge names, addresses or ASNs other than the two private-range ones
  (`64512`, `64601`) in any committed doc — use `${placeholder}` and `x.x.x.N`.
  `snoop-a2o` and `phoenix-1` are already public and fine. The pod CIDR
  `10.42.0.0/16` is a deliberate cleartext constant.
- **Cross-references.** Link ADRs as `ADR-NNNN` with a relative link. Code paths
  are backticked and repo-relative. Section numbers inside a doc are fine to
  cite within that doc; from code comments, cite the file (and ADR) rather than
  a `§` that will drift.
- **`CLAUDE.md` files** are for the facts that are true *regardless of task* in
  that subtree plus a short "current state / next" pointer. They load into every
  Claude session that touches the subtree, so they should be short; anything
  long goes in a reference doc, an ADR, or the worklog, and the `CLAUDE.md`
  points at it.
