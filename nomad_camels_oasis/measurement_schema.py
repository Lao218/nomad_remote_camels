"""ELN schema that triggers a CAMELS action only after an explicit request."""

from __future__ import annotations

import json

from nomad.actions.manager import get_action_status, start_action
from nomad.datamodel.data import EntryData
from nomad.datamodel.metainfo.annotations import ELNAnnotation, ELNComponentEnum
from nomad.metainfo import MEnum, Quantity, SchemaPackage

from nomad_camels_oasis.actions.models import CAMELSMeasurementInput

m_package = SchemaPackage()


class CAMELSMeasurement(EntryData):
    """A parameterised request to run one CAMELS protocol."""

    protocol_name = Quantity(
        type=str,
        description="Name of the remotely exposed CAMELS protocol.",
        a_eln=ELNAnnotation(component=ELNComponentEnum.StringEditQuantity),
    )
    variables_json = Quantity(
        type=str,
        default="{}",
        description="JSON object with the CAMELS protocol variable values.",
        a_eln=ELNAnnotation(component=ELNComponentEnum.RichTextEditQuantity),
    )
    measurement_status = Quantity(
        type=MEnum("draft", "ready", "queued", "running", "finished", "failed"),
        default="draft",
        a_eln=ELNAnnotation(component=ELNComponentEnum.EnumEditQuantity),
    )
    start_measurement = Quantity(
        type=bool,
        description="Explicitly queue this measurement in NOMAD Actions.",
        a_eln=ELNAnnotation(
            component=ELNComponentEnum.ActionEditQuantity,
            label="Start CAMELS measurement",
        ),
    )
    workflow_id = Quantity(type=str, description="NOMAD Action workflow ID.")
    workflow_status = Quantity(type=str, description="Latest NOMAD Action status.")
    camels_run_id = Quantity(type=str, description="Run ID returned by CAMELS.")

    def normalize(self, archive, logger):
        super().normalize(archive, logger)

        authors = archive.metadata.authors or []
        user_id = authors[0].user_id if authors else None

        if self.workflow_id and user_id:
            try:
                self.workflow_status = get_action_status(self.workflow_id, user_id).name
            except Exception as exc:  # Status lookup must never prevent saving an ELN.
                logger.warning("Could not retrieve CAMELS action status", exc_info=exc)

        requested = self.start_measurement or self.measurement_status == "ready"
        if not requested or self.workflow_id:
            return
        if not self.protocol_name:
            logger.warning("CAMELS measurement was requested without a protocol name.")
            return
        try:
            variables = json.loads(self.variables_json or "{}")
            if not isinstance(variables, dict):
                raise ValueError("variables_json must contain a JSON object")
        except (json.JSONDecodeError, ValueError) as exc:
            self.measurement_status = "failed"
            logger.error("Invalid CAMELS variables", exc_info=exc)
            return

        if not user_id or not archive.metadata.upload_id:
            logger.error("CAMELS Action requires an entry author and upload ID.")
            return

        self.workflow_id = start_action(
            action_id="nomad_camels_oasis.entry_points:camels_measurement_action",
            data=CAMELSMeasurementInput(
                user_id=user_id,
                upload_id=archive.metadata.upload_id,
                protocol_name=self.protocol_name,
                variables=variables,
            ),
        )
        self.workflow_status = "RUNNING"
        self.measurement_status = "queued"
        self.start_measurement = False


m_package.__init_metainfo__()
