"""NOMAD plugin entry point for the CAMELS bridge."""

from __future__ import annotations

import os

from pydantic import Field

from nomad.config.models.plugins import APIEntryPoint


class CAMELSAPIEntryPoint(APIEntryPoint):
    """Configuration for the OASIS-side CAMELS API proxy."""

    camels_base_url: str = Field(
        "http://camels:8000",
        description="Base URL of the CAMELS FastAPI service reachable by OASIS."
    )
    request_timeout_seconds: float = Field(10, gt=0, le=120)
    settings_path: str = Field("/app/.volumes/fs/camels-bridge/settings.json")

    def load(self):
        from nomad_camels_oasis.app import create_app

        return create_app(
            camels_base_url=self.camels_base_url,
            api_key=os.environ.get("CAMELS_API_KEY"),
            timeout_seconds=self.request_timeout_seconds,
            settings_path=self.settings_path,
            encryption_secret=os.environ.get("NOMAD_SERVICES_API_SECRET"),
        )


camels = CAMELSAPIEntryPoint(
    name="CAMELS remote control",
    description="Authenticated OASIS backend proxy for selected CAMELS protocols.",
    prefix="camels",
)
