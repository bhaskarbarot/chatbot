"""
Unit tests for utils/masking.py

Tests all masking functions for correctness and no false positives.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.masking import (
    mask_objectid,
    mask_pan,
    mask_gst,
    mask_account_number,
    mask_dict,
    mask_response,
    mask_value,
)


# ── mask_objectid ─────────────────────────────────────

class TestMaskObjectId:
    def test_24_char_hex_masked(self):
        oid = "507f1f77bcf86cd799439011"
        result = mask_objectid(oid)
        assert result == "507f1f77bcf86cd7994xxxxx"
        assert len(result) == 24

    def test_non_objectid_unchanged(self):
        text = "hello world 12345"
        assert mask_objectid(text) == text

    def test_objectid_in_sentence(self):
        text = "Document ID: 507f1f77bcf86cd799439011 is active"
        result = mask_objectid(text)
        assert "xxxxx" in result
        assert "507f1f77bcf86cd799439011" not in result

    def test_multiple_objectids_masked(self):
        text = "507f1f77bcf86cd799439011 and 507f1f77bcf86cd799439012"
        result = mask_objectid(text)
        assert result.count("xxxxx") == 2

    def test_23_char_not_masked(self):
        text = "507f1f77bcf86cd79943901"  # 23 chars
        assert mask_objectid(text) == text


# ── mask_pan ──────────────────────────────────────────

class TestMaskPan:
    def test_valid_pan_masked(self):
        pan = "ABCDE1234F"
        result = mask_pan(pan)
        assert result == "ABCDExxxxx"
        assert len(result) == 10

    def test_lowercase_not_masked(self):
        # PAN must be uppercase per regex
        text = "abcde1234f"
        assert mask_pan(text) == text

    def test_pan_in_sentence(self):
        text = "PAN number ABCDE1234F found"
        result = mask_pan(text)
        assert "ABCDE1234F" not in result
        assert "xxxxx" in result

    def test_invalid_format_unchanged(self):
        text = "ABCD12345F"  # wrong format (4 letters, not 5)
        assert mask_pan(text) == text


# ── mask_gst ──────────────────────────────────────────

class TestMaskGst:
    def test_valid_gst_masked(self):
        gst = "22ABCDE1234F1Z5"
        result = mask_gst(gst)
        assert result.endswith("xxxxx")
        assert len(result) == 15

    def test_invalid_gst_unchanged(self):
        text = "not-a-gst-number"
        assert mask_gst(text) == text


# ── mask_account_number ───────────────────────────────

class TestMaskAccountNumber:
    def test_10_digit_number_masked(self):
        result = mask_account_number("1234567890")
        assert result.endswith("7890")
        assert "x" in result

    def test_short_number_unchanged(self):
        result = mask_account_number("1234567")  # 7 digits, below threshold
        assert result == "1234567"

    def test_phone_in_sentence_not_mangled(self):
        # A 10-digit number in text should be masked (it matches the pattern)
        text = "Account: 1234567890"
        result = mask_account_number(text)
        assert result.endswith("7890")


# ── mask_dict ─────────────────────────────────────────

class TestMaskDict:
    def test_password_redacted(self):
        doc = {"name": "Test", "password": "secret123"}
        result = mask_dict(doc)
        assert result["password"] == "[REDACTED]"
        assert result["name"] == "Test"

    def test_google_token_redacted(self):
        doc = {"googleAccessToken": "ya29.something"}
        result = mask_dict(doc)
        assert result["googleAccessToken"] == "[REDACTED]"

    def test_objectid_field_masked(self):
        doc = {"_id": "507f1f77bcf86cd799439011"}
        result = mask_dict(doc)
        assert "xxxxx" in result["_id"]

    def test_bank_details_redacted(self):
        doc = {"bankDetails": {"account": "123", "ifsc": "ABC"}}
        result = mask_dict(doc)
        # All bank fields should be [REDACTED]
        for v in result["bankDetails"].values():
            assert v == "[REDACTED]"

    def test_nested_dict_recursed(self):
        doc = {"address": {"zip": "12345", "street": "Main St"}}
        result = mask_dict(doc)
        assert result["address"]["zip"] == "12345"
        assert result["address"]["street"] == "Main St"

    def test_non_sensitive_fields_unchanged(self):
        doc = {"name": "Acme Corp", "stage": "Proposal", "amount": 5000}
        result = mask_dict(doc)
        assert result["name"] == "Acme Corp"
        assert result["stage"] == "Proposal"
        assert result["amount"] == 5000


# ── mask_response ─────────────────────────────────────

class TestMaskResponse:
    def test_objectid_in_response_masked(self):
        text = "Found document 507f1f77bcf86cd799439011 in database."
        result = mask_response(text)
        assert "507f1f77bcf86cd799439011" not in result
        assert "xxxxx" in result

    def test_pan_in_response_masked(self):
        text = "Customer PAN: ABCDE1234F"
        result = mask_response(text)
        assert "ABCDE1234F" not in result

    def test_clean_response_unchanged(self):
        text = "Found 5 deals in Proposal stage."
        assert mask_response(text) == text

    def test_empty_string(self):
        assert mask_response("") == ""


# ── mask_value ────────────────────────────────────────

class TestMaskValue:
    def test_none_passthrough(self):
        assert mask_value(None) is None

    def test_string_with_objectid(self):
        result = mask_value("507f1f77bcf86cd799439011")
        assert "xxxxx" in result

    def test_list_recursed(self):
        data = ["507f1f77bcf86cd799439011", "normal"]
        result = mask_value(data)
        assert "xxxxx" in result[0]
        assert result[1] == "normal"

    def test_dict_recursed(self):
        # mask_dict masks specific ObjectId field names; use "_id" which is in the list
        data = {"_id": "507f1f77bcf86cd799439011"}
        result = mask_value(data)
        assert "xxxxx" in result["_id"]

    def test_non_string_passthrough(self):
        assert mask_value(42) == 42
        assert mask_value(3.14) == 3.14
