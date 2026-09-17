# -*- coding: UTF-8 -*-
"""常用地点接口模型。"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SavedLocationCreate(BaseModel):
    label: str = Field(min_length=1, max_length=50)
    address: Optional[str] = Field(default=None, max_length=200)
    city: Optional[str] = Field(default=None, max_length=100)
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-90, le=90)
    is_default: bool = False

    @field_validator("label")
    @classmethod
    def _strip_label(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("地点名称不能为空")
        return value

    @field_validator("address", "city")
    @classmethod
    def _strip_optional_text(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() or None if value is not None else None


class SavedLocationUpdate(BaseModel):
    label: Optional[str] = Field(default=None, min_length=1, max_length=50)
    address: Optional[str] = Field(default=None, max_length=200)
    city: Optional[str] = Field(default=None, max_length=100)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    is_default: Optional[bool] = None

    @field_validator("label")
    @classmethod
    def _strip_label(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("地点名称不能为空")
        return value

    @field_validator("address", "city")
    @classmethod
    def _strip_optional_text(cls, value: Optional[str]) -> Optional[str]:
        return value.strip() or None if value is not None else None

    @model_validator(mode="after")
    def _reject_null_required_fields(self):
        for field in ("label", "longitude", "latitude"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} 不能设为空")
        return self


class SavedLocationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    address: Optional[str]
    city: Optional[str]
    longitude: float
    latitude: float
    is_default: bool
    created_at: datetime
    updated_at: datetime
