#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Fetch every Bitwarden Secrets Manager secret the machine account can see, in
one API call, as a name -> value dict.

WHY THIS EXISTS instead of the official `bitwarden.secrets.lookup`:

  * That lookup takes ONE SECRET UUID PER CALL. It has no name lookup and no
    list operation. Using it per-variable would mean ~22 API calls AND ~22
    UUIDs replacing readable names in group_vars — worse than what it replaces
    on both counts.
  * BWS enforces UNDOCUMENTED rate limits. Bitwarden's own forum has reports of
    throttling after six calls in succession, including one from "an Ansible
    playbook which looks up a dozen or so secrets in quick succession" — this
    exact workload. Design for one bulk fetch, not for a threshold nobody
    publishes.
  * The limitation is the PLUGIN's, not BWS's. The SDK underneath already
    exposes sync()/list()/get_by_ids(); the collection just doesn't surface
    them. So the fix belongs here, in a thin wrapper over Bitwarden's own SDK,
    rather than in contorting the secret layout to fit a plugin's addressing
    model (e.g. hand-authoring JSON blobs into a textarea with no validation).

Full rationale + the alternatives rejected: docs/mac-studio-inference-stack-2.md,
Appendix A, "Control-node secrets".

⚠ NO STATE FILE IS WRITTEN, deliberately. `login_access_token()` takes an
optional state_file; we never pass one. State files exist to reduce rate
limiting ACROSS MANY AUTHENTICATIONS, and this module authenticates exactly once
per run, so the benefit is nil. Bitwarden documents them only as "fully
encrypted files that store authentication tokens and additional relevant data" —
neither the validity period nor the encrypting key is published. Persisting an
unspecified encrypted blob would undercut the whole point of retiring vault.yml,
which was to stop keeping long-lived secret material on disk.
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: bws_secrets
short_description: Bulk-fetch Bitwarden Secrets Manager secrets as a name/value dict
description:
  - Authenticates once with a machine-account access token and returns every
    secret that account can read, keyed by secret name.
  - Uses a single sync() call rather than one request per secret, because BWS
    enforces undocumented rate limits that per-secret lookups trip.
  - Never writes an SDK state file.
options:
  access_token:
    description: BWS machine-account access token. Read-only, scoped to one project.
    required: true
    type: str
  organization_id:
    description: Bitwarden organization UUID. Not a credential, but environment-identifying.
    required: true
    type: str
  project_id:
    description:
      - Optional project UUID to restrict results to. Omit to accept everything
        the machine account can read.
      - Filtering happens client-side; the token's own scoping is the real control.
    required: false
    type: str
  base_url:
    description: Overrides api_url/identity_url for self-hosted Bitwarden.
    required: false
    type: str
  api_url:
    description: API base URL.
    required: false
    type: str
    default: https://api.bitwarden.com
  identity_url:
    description: Identity base URL.
    required: false
    type: str
    default: https://identity.bitwarden.com
requirements:
  - bitwarden-sdk
author:
  - homelab-infra
"""

EXAMPLES = r"""
- name: Fetch all BWS secrets
  bws_secrets:
    access_token: "{{ bws_access_token }}"
    organization_id: "{{ bws_organization_id }}"
  register: bws_result
  no_log: true
"""

RETURN = r"""
secrets:
  description: Mapping of secret name to secret value.
  returned: always
  type: dict
count:
  description: Number of secrets returned (safe to log — no values).
  returned: always
  type: int
names:
  description: Sorted secret names (safe to log — no values). Useful for debugging a missing key.
  returned: always
  type: list
  elements: str
"""

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

BW_SDK_IMPORT_ERROR = None
try:
    from bitwarden_sdk import BitwardenClient, DeviceType, client_settings_from_dict
except ImportError as exc:  # pragma: no cover
    BW_SDK_IMPORT_ERROR = exc


def _unwrap(response):
    """SDK responses are dataclasses with .to_dict() -> {"data": ..., ...}.

    The official lookup does exactly `secret.to_dict()["data"][field]`, so this
    envelope is the documented-by-usage shape. Tolerate a plain dict too, in
    case a future SDK returns one directly.
    """
    raw = response.to_dict() if hasattr(response, "to_dict") else response
    if not isinstance(raw, dict):
        raise ValueError("unexpected SDK response type: %s" % type(raw).__name__)
    if raw.get("success") is False:
        raise ValueError(raw.get("errorMessage") or "BWS reported failure with no message")
    return raw.get("data")


def _secrets_from_sync(data):
    """sync() data carries the secret list. Key name is not in any published
    schema we can pin to, so probe the plausible spellings and fail loudly with
    what we actually got rather than silently returning nothing."""
    if data is None:
        return None
    for key in ("secrets", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    return None


def run_module():
    module = AnsibleModule(
        argument_spec=dict(
            access_token=dict(type="str", required=True, no_log=True),
            organization_id=dict(type="str", required=True),
            project_id=dict(type="str", required=False),
            base_url=dict(type="str", required=False),
            api_url=dict(type="str", required=False, default="https://api.bitwarden.com"),
            identity_url=dict(
                type="str", required=False, default="https://identity.bitwarden.com"
            ),
        ),
        # Read-only: nothing is created or changed, so check mode is a no-op run.
        supports_check_mode=True,
    )

    if BW_SDK_IMPORT_ERROR is not None:
        module.fail_json(
            msg=missing_required_lib("bitwarden-sdk"), exception=str(BW_SDK_IMPORT_ERROR)
        )

    p = module.params
    api_url = p["base_url"] or p["api_url"]
    identity_url = p["base_url"] or p["identity_url"]

    client = BitwardenClient(
        client_settings_from_dict(
            {
                "apiUrl": api_url,
                "identityUrl": identity_url,
                "deviceType": DeviceType.SDK,
                "userAgent": "homelab-infra/bws_secrets",
            }
        )
    )

    # ⚠ state_file is deliberately omitted — see the module docstring.
    try:
        client.auth().login_access_token(p["access_token"])
    except Exception as exc:
        module.fail_json(
            msg=(
                "BWS authentication failed. Check the access token is current and "
                "not expired, and that it belongs to organization %s. Error: %s"
                % (p["organization_id"], exc)
            )
        )

    # ONE call for everything. sync() with no last_synced_date returns every
    # secret the machine account can read, values included.
    try:
        data = _unwrap(client.secrets().sync(p["organization_id"], None))
        entries = _secrets_from_sync(data)
    except Exception as exc:
        hint = ""
        # Auth already succeeded by this point, so a 404 is the ORG id not
        # resolving — not a permissions problem (that would be 403). The usual
        # cause is passing the PROJECT uuid here; both are uuids and they get
        # supplied one line apart.
        if "404" in str(exc) or "not found" in str(exc).lower():
            hint = (
                " — the access token authenticated fine, so this is almost"
                " certainly a wrong organization_id ('%s'). Note that is the"
                " ORGANIZATION uuid, NOT the project uuid: find it in the web"
                " vault URL when viewing the organization"
                " (/organizations/<uuid>/...). Set it via BWS_ORG_ID or"
                " -e bws_organization_id=..." % p["organization_id"]
            )
        module.fail_json(msg="BWS sync() failed: %s%s" % (exc, hint))

    # Fallback: list() ids then get_by_ids() values — two calls instead of one,
    # still bounded. Only used if sync()'s payload shape isn't what we expect,
    # so a future SDK change degrades rather than breaks.
    if entries is None:
        try:
            ids_data = _unwrap(client.secrets().list(p["organization_id"])) or {}
            ids = [item["id"] for item in (ids_data.get("data") or [])]
            if not ids:
                entries = []
            else:
                values = _unwrap(client.secrets().get_by_ids(ids)) or {}
                entries = values.get("data") or []
        except Exception as exc:
            module.fail_json(
                msg=(
                    "BWS sync() returned an unrecognised payload and the "
                    "list()/get_by_ids() fallback also failed: %s" % exc
                )
            )

    if p["project_id"]:
        entries = [e for e in entries if e.get("projectId") == p["project_id"]]

    secrets = {}
    duplicates = []
    for entry in entries:
        name = entry.get("key")
        if name is None:
            continue
        if name in secrets:
            duplicates.append(name)
        secrets[name] = entry.get("value")

    # Two secrets with the same name in scope means one silently wins and a
    # value you never see is in play. Refuse rather than pick.
    if duplicates:
        module.fail_json(
            msg=(
                "Duplicate secret name(s) visible to this machine account: %s. "
                "Names must be unique within the accessible scope — rename or "
                "narrow project_id." % ", ".join(sorted(set(duplicates)))
            )
        )

    module.exit_json(
        changed=False,
        secrets=secrets,
        count=len(secrets),
        names=sorted(secrets.keys()),
    )


def main():
    run_module()


if __name__ == "__main__":
    main()
