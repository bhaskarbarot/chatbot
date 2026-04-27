"""
Masking utility for sensitive data.
Masks ObjectIds, PAN numbers, GST numbers, and other sensitive identifiers.
Rules:
  - ObjectId (24 hex chars): mask last 5 chars with 'x'
  - PAN (AAACU0564G):        mask last 5 chars with 'x'
  - GST (22AAAAA0000A1Z5):   mask last 5 chars with 'x'
  - Account numbers:         mask all but last 4 digits
"""
import re
from typing import Any, Union


def mask_objectid(value: str) -> str:
    """Mask MongoDB ObjectId: keep first 19, replace last 5 with 'x'."""
    pattern = re.compile(r'\b[0-9a-fA-F]{24}\b')
    def replacer(m):
        oid = m.group(0)
        return oid[:-5] + "xxxxx"
    return pattern.sub(replacer, str(value))


def mask_pan(value: str) -> str:
    """Mask PAN numbers: AAACU0564G -> AAACUxxxxx."""
    pattern = re.compile(r'\b[A-Z]{5}[0-9]{4}[A-Z]\b')
    def replacer(m):
        pan = m.group(0)
        return pan[:-5] + "xxxxx"
    return pattern.sub(replacer, str(value))


def mask_gst(value: str) -> str:
    """Mask GST numbers: last 5 chars masked."""
    pattern = re.compile(r'\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]{3}\b')
    def replacer(m):
        gst = m.group(0)
        return gst[:-5] + "xxxxx"
    return pattern.sub(replacer, str(value))


def mask_account_number(value: str) -> str:
    """Mask bank account numbers: show only last 4 digits."""
    pattern = re.compile(r'\b\d{8,18}\b')
    def replacer(m):
        num = m.group(0)
        if len(num) >= 8:
            return "x" * (len(num) - 4) + num[-4:]
        return num
    return pattern.sub(replacer, str(value))


def mask_value(value: Any) -> Any:
    """Apply all masking rules to a single value."""
    if value is None:
        return None
    if isinstance(value, dict):
        return mask_dict(value)
    if isinstance(value, list):
        return [mask_value(item) for item in value]
    if isinstance(value, str):
        result = value
        result = mask_objectid(result)
        result = mask_pan(result)
        result = mask_gst(result)
        return result
    return value


def mask_dict(data: dict) -> dict:
    """Recursively mask all sensitive values in a dictionary."""
    masked = {}
    for key, val in data.items():
        if key == "password" or key == "googleAccessToken" or key == "tokens":
            masked[key] = "[REDACTED]"
        elif key in ("_id", "owner", "company", "contact", "createdBy",
                      "invoiceOwner", "salesOwner", "companyOwner",
                      "contactOwner", "assignedTo", "createdByOwner",
                      "business_analyst", "reporting_manager",
                      "so_number", "dealsId", "companyId", "contectId",
                      "invoiceId", "salesId", "userId", "campaign",
                      "region", "regionId", "source", "department",
                      "product_type", "category"):
            masked[key] = mask_objectid(str(val)) if val else val
        elif key in ("PAN", "GSTIN", "gst"):
            masked[key] = mask_pan(str(val)) if val else val
        elif isinstance(val, dict):
            if key == "bankDetails":
                masked[key] = {k: "[REDACTED]" for k in val} if val else val
            else:
                masked[key] = mask_dict(val)
        elif isinstance(val, list):
            masked[key] = [mask_value(item) for item in val]
        else:
            masked[key] = val
    return masked


def mask_response(text: str) -> str:
    """Apply all masking to a text response string."""
    result = text
    result = mask_objectid(result)
    result = mask_pan(result)
    result = mask_gst(result)
    return result
