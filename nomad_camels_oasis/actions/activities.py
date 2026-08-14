"""Temporal activities which communicate with the configured CAMELS service."""

from __future__ import annotations

import os
from typing import Any

import requests
from temporalio import activity

from nomad_camels_oasis.app import CredentialStore
from nomad_camels_oasis.actions.models import CAMELSMeasurementInput


def _connection_for_protocol(user_id: str, protocol_name: str) -> tuple[str, str]:
    """Read one user's encrypted, protocol-scoped CAMELS connection."""
    path = os.environ.get(
        "CAMELS_SETTINGS_PATH", "/app/.volumes/fs/camels-bridge/settings.json"
    )
    secret = os.environ.get("NOMAD_SERVICES_API_SECRET")
    saved = CredentialStore(path, secret).load()
    users = saved.get("users", {})
    connections = users.get(user_id, {}).get("connections", {}) if isinstance(users, dict) else {}
    for connection in connections.values():
        if connection.get("protocol_name") == protocol_name:
            return connection["camels_base_url"].rstrip("/"), connection["api_key"]
    raise ValueError(f"No saved CAMELS API key exists for protocol {protocol_name!r}.")


@activity.defn
def start_camels_run(data: CAMELSMeasurementInput) -> dict[str, Any]:
    base_url, api_key = _connection_for_protocol(data.user_id, data.protocol_name)
    response = requests.post(
        f"{base_url}/api/v1/actions/run/protocols/{data.protocol_name}",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"variables": data.variables},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()


@activity.defn
def get_camels_run_status(data: dict[str, str]) -> dict[str, Any]:
    base_url, api_key = _connection_for_protocol(data["user_id"], data["protocol_name"])
    response = requests.get(
        f"{base_url}/api/v1/protocols/results/{data['run_id']}",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20,
    )
    response.raise_for_status()
    return response.json()
