"""Optional checks against private local samples; expectations stay outside Git."""

import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from invoice_tool.models import InvoiceRecord
from invoice_tool.service import InvoiceService
from invoice_tool.validation import validate_invoice


def _sample(variable: str) -> Path:
    value = os.getenv(variable)
    if not value:
        pytest.skip(f"未提供 {variable}")
    path = Path(value)
    if not path.is_file():
        pytest.skip(f"{variable} 指定的本地样张不存在")
    return path


def _expected(variable: str) -> dict:
    value = os.getenv(variable)
    if not value:
        pytest.skip(f"未提供 {variable}")
    try:
        expected = json.loads(value)
    except json.JSONDecodeError:
        pytest.fail(f"{variable} 必须是有效的 JSON 对象", pytrace=False)
    if not isinstance(expected, dict) or not {"invoice_number", "total_with_tax", "items"} <= expected.keys():
        pytest.fail(f"{variable} 缺少 invoice_number、total_with_tax 或 items", pytrace=False)
    if not isinstance(expected["items"], list) or not expected["items"]:
        pytest.fail(f"{variable} 的 items 必须是非空列表", pytrace=False)
    allowed_fields = {"project_name", "specification", "unit", "quantity", "unit_price", "amount", "tax_rate", "tax_amount"}
    if any(not isinstance(item, dict) or "amount" not in item or not item.keys() <= allowed_fields for item in expected["items"]):
        pytest.fail(f"{variable} 的商品字段无效或缺少 amount", pytrace=False)
    return expected


def _assert_expected(record: InvoiceRecord, expected: dict) -> None:
    assert record.invoice_number == str(expected["invoice_number"])
    assert record.total_with_tax == Decimal(str(expected["total_with_tax"]))
    assert len(record.items) == len(expected["items"])
    decimal_fields = {"quantity", "unit_price", "amount", "tax_rate", "tax_amount"}
    for actual_item, expected_item in zip(record.items, expected["items"], strict=True):
        for field, value in expected_item.items():
            expected_value = Decimal(str(value)) if field in decimal_fields else value
            assert getattr(actual_item, field) == expected_value
    assert validate_invoice(record).valid


def _check_sample(sample_variable: str, expected_variable: str) -> None:
    sample = _sample(sample_variable)
    expected = _expected(expected_variable)
    service = InvoiceService()
    try:
        _assert_expected(service.parse_file(sample), expected)
    finally:
        service.cleanup()


def test_pdf_sample() -> None:
    _check_sample("INVOICE_PDF_SAMPLE", "INVOICE_PDF_EXPECTED")


def test_image_sample() -> None:
    _check_sample("INVOICE_IMAGE_SAMPLE", "INVOICE_IMAGE_EXPECTED")


def test_screenshot_image_with_inner_table_border() -> None:
    _check_sample("INVOICE_IMAGE_INNER_TABLE_SAMPLE", "INVOICE_IMAGE_INNER_TABLE_EXPECTED")
