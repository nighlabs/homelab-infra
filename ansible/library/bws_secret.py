#!/usr/bin/python
# -*- coding: utf-8 -*-
"""Create ONE Bitwarden Secrets Manager secret if it does not already exist.

Companion to bws_secrets.py (which bulk-READS). This one WRITES, and exists
solely so the vault -> BWS migration can be a command rather than 23 manual
pastes into a web form — which is the highest-transcription-risk step in the
whole move.

⚠ REQUIRES A WRITE-SCOPED ACCESS TOKEN, unlike everything else in this repo.
Use a SEPARATE, TEMPORARY machine account for the import and delete it
afterwards; never widen the production read-only token. The runtime token that
plays use must stay read-only.

IDEMPOTENT BY NAME. The caller passes `existing_names` (from one bws_secrets
fetch), so this never issues a read per secret — and a re-run after a partial
failure creates only what's missing. That matters more than it looks: BWS
enforces undocumented rate limits (reports of throttling after ~6 calls in
succession), so a 23-secret import can plausibly be interrupted part-way.
Resumability is the answer, not hoping it fits under an unpublished ceiling.

There is no bulk create in the SDK, so creation is inherently one call per
secret — pace it from the caller (loop_control.pause).
"""

from __future__ import absolute_import, division, print_function

__metaclass__ = type

DOCUMENTATION = r"""
---
module: bws_secret
short_description: Create a Bitwarden Secrets Manager secret if absent
description:
  - Creates one secret, keyed by name, in the given organization and project.
  - Skips (reporting not-changed) when the name is already present, so runs are
    idempotent and resumable after a rate-limit interruption.
  - Never updates an existing secret — see the C(existing_names) note.
options:
  access_token:
    description: BWS access token with WRITE access. Use a temporary machine account.
    required: true
    type: str
  organization_id:
    description: Bitwarden organization UUID.
    required: true
    type: str
  project_id:
    description: Project UUID to file the secret under.
    required: true
    type: str
  key:
    description: Secret name.
    required: true
    type: str
  value:
    description: Secret value.
    required: true
    type: str
  note:
    description: Optional note stored alongside the secret.
    required: false
    type: str
  existing_names:
    description:
      - Names already present, from a single bulk read. Used to decide
        create-vs-skip without a read per secret.
      - If omitted, the module reads the org once itself.
    required: false
    type: list
    elements: str
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
- name: Create a secret if absent
  bws_secret:
    access_token: "{{ bws_write_token }}"
    organization_id: "{{ bws_organization_id }}"
    project_id: "{{ bws_project_id }}"
    key: proxmox_api_host
    value: "10.0.0.11"
    existing_names: "{{ bws_existing }}"
  no_log: true
"""

RETURN = r"""
created:
  description: Whether a secret was created (false means it already existed).
  returned: always
  type: bool
key:
  description: The secret name acted on (safe to log — no value).
  returned: always
  type: str
"""

from ansible.module_utils.basic import AnsibleModule, missing_required_lib

BW_SDK_IMPORT_ERROR = None
try:
    from bitwarden_sdk import BitwardenClient, DeviceType, client_settings_from_dict
except ImportError as exc:  # pragma: no cover
    BW_SDK_IMPORT_ERROR = exc


def _unwrap(response):
    raw = response.to_dict() if hasattr(response, "to_dict") else response
    if not isinstance(raw, dict):
        raise ValueError("unexpected SDK response type: %s" % type(raw).__name__)
    if raw.get("success") is False:
        raise ValueError(raw.get("errorMessage") or "BWS reported failure with no message")
    return raw.get("data")


def run_module():
    module = AnsibleModule(
        argument_spec=dict(
            access_token=dict(type="str", required=True, no_log=True),
            organization_id=dict(type="str", required=True),
            project_id=dict(type="str", required=True),
            key=dict(type="str", required=True),
            value=dict(type="str", required=True, no_log=True),
            note=dict(type="str", required=False),
            existing_names=dict(type="list", elements="str", required=False),
            api_url=dict(type="str", required=False, default="https://api.bitwarden.com"),
            identity_url=dict(
                type="str", required=False, default="https://identity.bitwarden.com"
            ),
        ),
        supports_check_mode=True,
    )

    if BW_SDK_IMPORT_ERROR is not None:
        module.fail_json(
            msg=missing_required_lib("bitwarden-sdk"), exception=str(BW_SDK_IMPORT_ERROR)
        )

    p = module.params
    key = p["key"]

    # Decide from the caller-supplied list when we have it — no read per secret.
    if p["existing_names"] is not None and key in p["existing_names"]:
        module.exit_json(changed=False, created=False, key=key)

    if module.check_mode:
        module.exit_json(changed=True, created=False, key=key)

    client = BitwardenClient(
        client_settings_from_dict(
            {
                "apiUrl": p["api_url"],
                "identityUrl": p["identity_url"],
                "deviceType": DeviceType.SDK,
                "userAgent": "homelab-infra/bws_secret",
            }
        )
    )

    # No state_file — same reasoning as bws_secrets.py.
    try:
        client.auth().login_access_token(p["access_token"])
    except Exception as exc:
        module.fail_json(
            msg=(
                "BWS authentication failed for the WRITE token. This module needs a "
                "token with write access, which is deliberately NOT the token plays "
                "use at run time. Error: %s" % exc
            )
        )

    # Only if the caller gave us nothing to compare against.
    if p["existing_names"] is None:
        try:
            data = _unwrap(client.secrets().list(p["organization_id"])) or {}
            if any(i.get("key") == key for i in (data.get("data") or [])):
                module.exit_json(changed=False, created=False, key=key)
        except Exception as exc:
            module.fail_json(msg="Could not list existing secrets: %s" % exc)

    try:
        _unwrap(
            client.secrets().create(
                p["organization_id"], key, p["value"], p.get("note"), [p["project_id"]]
            )
        )
    except Exception as exc:
        module.fail_json(
            msg=(
                "Failed to create secret '%s': %s. If this is a rate limit, just "
                "re-run — already-created secrets are skipped." % (key, exc)
            )
        )

    module.exit_json(changed=True, created=True, key=key)


def main():
    run_module()


if __name__ == "__main__":
    main()
