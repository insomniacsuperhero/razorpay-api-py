from typing import Literal

from pydantic import BaseModel, Field


class ExchangeIn(BaseModel):
    token: str


class UploadIn(BaseModel):
    filename: str = Field(max_length=200)
    mime_type: str
    size_bytes: int


class CompleteIn(BaseModel):
    etag: str | None = None


class EvidencePatch(BaseModel):
    fact_key: str | None = None
    value: str | int | None = None
    final_field: str | None = None
    confirm: bool | None = None
    answer: Literal["yes", "no"] | None = None
    clear_bank_flag: bool | None = None

    def as_body(self) -> dict:
        d = self.model_dump(exclude_unset=True)
        if "fact_key" not in d:
            d.pop("value", None)
        return d


class DraftIn(BaseModel):
    summary: str | None = Field(None, max_length=1000)
    contested_amount: int | None = None


class ContestIn(BaseModel):
    action: Literal["draft", "submit"]


class MetricIn(BaseModel):
    event_name: str = Field(max_length=64, pattern=r"^[a-z0-9_.:-]+$")
    metadata: dict = Field(default_factory=dict)


class SeedIn(BaseModel):
    payment_id: str | None = Field(None, description="Captured test payment; defaults to the last one seen by webhook")
    reason_code: str = "goods_not_received"
    respond_in_hours: float = 72
    amount: int | None = None


class ActionRequiredIn(BaseModel):
    field: str | None = "shipping_proof"
    reason: str | None = "The delivery proof is unclear. Please upload a clearer courier screenshot."
    respond_in_hours: float = 48


class ResolveIn(BaseModel):
    outcome: Literal["won", "lost"]
