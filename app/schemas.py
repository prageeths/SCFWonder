"""API-level Pydantic schemas for SCF Wonder."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class CompanyOnboard(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    country: str = Field(..., min_length=2, max_length=64)
    industry: Optional[str] = Field(default=None, max_length=128)
    annual_revenue_usd: float = Field(..., gt=0)
    years_operated: int = Field(..., ge=0, le=200)
    role: str = Field(default="BOTH")
    parent_name: Optional[str] = None


class InvoiceCreate(BaseModel):
    seller_name: str
    buyer_name: str
    product: str
    amount: float = Field(..., gt=0)
    currency: str
    tenor_days: int
    grace_period_days: int = 0
    invoice_number: Optional[str] = None
    new_seller: Optional[CompanyOnboard] = None
    new_buyer: Optional[CompanyOnboard] = None
