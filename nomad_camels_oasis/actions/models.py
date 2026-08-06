from typing import Any

from pydantic import BaseModel, Field


class CAMELSMeasurementInput(BaseModel):
    user_id: str
    upload_id: str
    protocol_name: str = Field(min_length=1)
    variables: dict[str, Any] = Field(default_factory=dict)
