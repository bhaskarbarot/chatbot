"""
Unit tests for utils/formatter.py

Tests: intent detection, format helpers, executive summary, auto_format routing.
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from utils.formatter import (
    detect_format_intent,
    format_date,
    format_currency,
    format_value,
    format_as_count,
    format_as_list,
    format_as_table,
    format_aggregation,
    auto_format,
    _detect_entity_type_from_query,
    _executive_summary_lines,
    _humanize_key,
)


# ── detect_format_intent ──────────────────────────────

class TestDetectFormatIntent:
    @pytest.mark.parametrize("query, expected", [
        ("show me a table of deals", "table"),
        ("in tabular format", "table"),
        ("how many invoices are there", "count"),
        ("count of pending tasks", "count"),
        ("give me full details", "detail"),
        ("complete information about this deal", "detail"),
        ("summarize the pipeline", "summary"),
        ("brief overview", "summary"),
        ("list all contacts", "list"),
        ("show all open deals", "list"),
        ("what is the total revenue", "auto"),  # no keyword match → auto
    ])
    def test_intent_detection(self, query, expected):
        assert detect_format_intent(query) == expected


# ── format_date ───────────────────────────────────────

class TestFormatDate:
    def test_none_returns_na(self):
        assert format_date(None) == "N/A"

    def test_iso_string_formatted(self):
        result = format_date("2025-03-15T00:00:00")
        assert "15" in result and ("Mar" in result or "03" in result)

    def test_already_formatted_string_returned(self):
        result = format_date("some-non-date-string")
        assert result == "some-non-date-string"

    def test_datetime_object_formatted(self):
        from datetime import datetime
        dt = datetime(2025, 6, 1)
        result = format_date(dt)
        assert "01" in result and "Jun" in result


# ── format_currency ───────────────────────────────────

class TestFormatCurrency:
    def test_none_returns_na(self):
        assert format_currency(None) == "N/A"

    def test_large_value_formatted(self):
        result = format_currency(1500000)
        assert "$" in result
        assert "1,500,000" in result

    def test_small_value_formatted(self):
        result = format_currency(99.50)
        assert "$" in result
        assert "99.50" in result

    def test_string_value(self):
        result = format_currency("2500.00")
        assert "$" in result

    def test_invalid_value(self):
        result = format_currency("not-a-number")
        assert result == "not-a-number"


# ── format_value ──────────────────────────────────────

class TestFormatValue:
    def test_none_returns_na(self):
        assert format_value("anything", None) == "N/A"

    def test_empty_string_returns_na(self):
        assert format_value("name", "") == "N/A"

    def test_date_key_formats_date(self):
        result = format_value("createdAt", "2025-01-01T00:00:00")
        assert "2025" in result or "Jan" in result

    def test_currency_key_formats_currency(self):
        result = format_value("grand_total_in_usd", 10000)
        assert "$" in result

    def test_bool_true_shows_yes(self):
        assert format_value("isActive", True) == "Yes"

    def test_bool_false_shows_no(self):
        assert format_value("deleted", False) == "No"

    def test_list_of_strings_joined(self):
        result = format_value("tags", ["a", "b", "c"])
        assert "a" in result and "b" in result

    def test_empty_list_returns_na(self):
        assert format_value("items", []) == "N/A"


# ── auto_format ───────────────────────────────────────

class TestAutoFormat:
    def test_integer_data_routes_to_count(self):
        result = auto_format(42, "how many deals")
        assert "42" in result

    def test_empty_list_returns_no_results(self):
        result = auto_format([], "show deals")
        assert "No results" in result or "No" in result

    def test_list_data_returns_string(self):
        data = [{"name": "Deal A", "stage": "Proposal", "createdAt": "2025-01-01"}]
        result = auto_format(data, "show deals")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_aggregation_data_renders_table(self):
        data = [
            {"_id": "Proposal", "count": 5},
            {"_id": "Closed Won", "count": 3},
        ]
        result = auto_format(data, "pipeline by stage")
        assert "|" in result  # markdown table

    def test_table_hint_renders_table(self):
        data = [
            {"name": "A", "stage": "Proposal", "createdAt": "2025-01-01"},
            {"name": "B", "stage": "Closed", "createdAt": "2025-01-02"},
        ]
        result = auto_format(data, "deals", "table")
        assert "|" in result

    def test_dict_with_count_key_routes_to_count(self):
        result = auto_format({"count": 10}, "how many")
        assert "10" in result


# ── _executive_summary_lines ──────────────────────────

class TestExecutiveSummaryLines:
    def test_count_data_produces_3_plus_blank_lines(self):
        lines = _executive_summary_lines(5, "how many deals")
        assert len(lines) == 4  # 3 lines + empty
        assert "5" in lines[0]

    def test_empty_list_produces_no_results_line(self):
        lines = _executive_summary_lines([], "show deals")
        assert any("No" in l for l in lines)

    def test_non_empty_list_produces_summary(self):
        data = [{"name": "X", "stage": "Proposal"}]
        lines = _executive_summary_lines(data, "show deals")
        assert len(lines) >= 3
        assert any("1" in l for l in lines)

    def test_revenue_query_shows_total_if_present(self):
        data = [{"total_revenue_usd": 500000.0}]
        lines = _executive_summary_lines(data, "total revenue this year")
        combined = " ".join(lines)
        assert "$" in combined or "500" in combined


# ── _humanize_key ─────────────────────────────────────

class TestHumanizeKey:
    def test_known_mapping(self):
        assert _humanize_key("companyName") == "Company"
        assert _humanize_key("payment_status") == "Payment Status"

    def test_camel_case_split(self):
        # "dealWonAt" is in the known mapping → "Won Date"; use unmapped key
        result = _humanize_key("customFieldName")
        assert "Custom" in result and "Field" in result and "Name" in result

    def test_underscore_replaced(self):
        result = _humanize_key("invoice_date")
        assert "_" not in result
        assert "Invoice" in result and "Date" in result


# ── _detect_entity_type_from_query ────────────────────

class TestDetectEntityType:
    @pytest.mark.parametrize("query, expected", [
        ("show all deals", "deal"),
        ("total invoice amount", "invoice"),
        ("get contacts for company", "company"),  # "company" check precedes "contact" in formatter
        ("pending tasks this week", "task"),
        ("total revenue this year", "invoice"),
        ("who is the top sales rep", "user"),
        ("outreach campaign stats", "lead"),
        ("show targets for this month", "target"),
    ])
    def test_entity_detection(self, query, expected):
        assert _detect_entity_type_from_query(query) == expected
