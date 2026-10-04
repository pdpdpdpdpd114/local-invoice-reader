from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Iterable

from .models import InvoiceRecord, PurchaseApplication, ReconciliationResult, ValidationIssue, ValidationResult

MONEY_QUANTUM = Decimal("0.01")
TOLERANCE = Decimal("0.01")
REQUIRED_ITEM_FIELDS = (
    "project_name",
    "unit",
    "quantity",
    "unit_price",
    "amount",
    "tax_rate",
    "tax_amount",
)


def to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip().replace("￥", "").replace("¥", "").replace(",", "")
    if not text:
        return None
    if text.endswith("%"):
        text = text[:-1].strip()
        try:
            return Decimal(text) / Decimal("100")
        except InvalidOperation:
            return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def approximately_equal(left: Decimal, right: Decimal) -> bool:
    return abs(left - right) <= TOLERANCE


def validate_invoice(invoice: InvoiceRecord) -> ValidationResult:
    issues: list[ValidationIssue] = []
    if not invoice.invoice_number.strip():
        issues.append(ValidationIssue("invoice_number", "缺少发票号码。"))
    elif not invoice.invoice_number.strip().isdigit() or not 15 <= len(invoice.invoice_number.strip()) <= 25:
        issues.append(ValidationIssue("invoice_number", "发票号码应为 15 至 25 位数字。"))
    if invoice.total_with_tax is None:
        issues.append(ValidationIssue("total_with_tax", "缺少价税合计。"))
    if not invoice.items:
        issues.append(ValidationIssue("items", "未识别到商品明细。"))

    amount_values: list[Decimal] = []
    tax_values: list[Decimal] = []
    for index, item in enumerate(invoice.items):
        for field in REQUIRED_ITEM_FIELDS:
            value = getattr(item, field)
            if value is None or (isinstance(value, str) and not value.strip()):
                issues.append(ValidationIssue(field, f"第 {index + 1} 条明细缺少{_field_label(field)}。", item_index=index))
        if item.quantity is not None and item.unit_price is not None and item.amount is not None:
            if not approximately_equal(money(item.quantity * item.unit_price), money(item.amount)):
                issues.append(ValidationIssue("amount", f"第 {index + 1} 条：数量 × 单价与金额不一致。", item_index=index))
        if item.amount is not None and item.tax_rate is not None and item.tax_amount is not None:
            if not approximately_equal(money(item.amount * item.tax_rate), money(item.tax_amount)):
                issues.append(ValidationIssue("tax_amount", f"第 {index + 1} 条：金额 × 税率与税额不一致。", item_index=index))
        if item.amount is not None:
            amount_values.append(item.amount)
        if item.tax_amount is not None:
            tax_values.append(item.tax_amount)

    if amount_values:
        sum_amount = money(sum(amount_values, Decimal("0")))
        if invoice.amount_total is not None and not approximately_equal(sum_amount, money(invoice.amount_total)):
            issues.append(ValidationIssue("amount_total", "商品金额合计与票面金额合计不一致。"))
    else:
        sum_amount = None
    if tax_values:
        sum_tax = money(sum(tax_values, Decimal("0")))
        if invoice.tax_total is not None and not approximately_equal(sum_tax, money(invoice.tax_total)):
            issues.append(ValidationIssue("tax_total", "商品税额合计与票面税额合计不一致。"))
    else:
        sum_tax = None
    if sum_amount is not None and sum_tax is not None and invoice.total_with_tax is not None:
        if not approximately_equal(money(sum_amount + sum_tax), money(invoice.total_with_tax)):
            issues.append(ValidationIssue("total_with_tax", "金额合计加税额合计与价税合计不一致。"))
    return ValidationResult(issues)


def validate_duplicates(invoices: Iterable[InvoiceRecord]) -> dict[int, ValidationIssue]:
    seen: dict[str, int] = {}
    duplicate_issues: dict[int, ValidationIssue] = {}
    for index, invoice in enumerate(invoices):
        number = invoice.invoice_number.strip()
        if not number:
            continue
        if number in seen:
            duplicate_issues[index] = ValidationIssue("invoice_number", f"与第 {seen[number] + 1} 张发票号码重复。")
            duplicate_issues[seen[number]] = ValidationIssue("invoice_number", f"与第 {index + 1} 张发票号码重复。")
        else:
            seen[number] = index
    return duplicate_issues


def reconcile_application(application: PurchaseApplication, invoices: Iterable[InvoiceRecord]) -> ReconciliationResult:
    invoice_total = sum(((invoice.total_with_tax or Decimal("0")) for invoice in invoices), Decimal("0"))
    difference = None if application.purchase_amount is None else money(invoice_total - application.purchase_amount)
    return ReconciliationResult(application.purchase_amount, money(invoice_total), difference)


def validate_application(
    application: PurchaseApplication | None,
    invoices: Iterable[InvoiceRecord],
    *,
    duplicate_number: bool = False,
) -> ValidationResult:
    issues: list[ValidationIssue] = []
    if application is None:
        return ValidationResult([ValidationIssue("application", "缺少申请表信息。")])
    if not application.application_number.strip():
        issues.append(ValidationIssue("application_number", "缺少申请单编号。"))
    elif duplicate_number:
        issues.append(ValidationIssue("application_number", "申请单编号重复。"))
    if not application.applicant.strip():
        issues.append(ValidationIssue("applicant", "缺少申请人。"))
    if application.purchase_date is None:
        issues.append(ValidationIssue("purchase_date", "缺少采购时间。"))
    if not application.group_name.strip():
        issues.append(ValidationIssue("group_name", "缺少部门。"))
    if application.purchase_amount is None:
        issues.append(ValidationIssue("purchase_amount", "缺少采购单金额。"))
    reconciliation = reconcile_application(application, invoices)
    if not reconciliation.matched and not application.difference_acknowledged:
        issues.append(ValidationIssue("reconciliation", "发票总价与采购单金额不一致，需人工确认差异。"))
    return ValidationResult(issues)


def _field_label(field: str) -> str:
    return {
        "project_name": "项目名称",
        "specification": "规格型号",
        "unit": "单位",
        "quantity": "数量",
        "unit_price": "单价",
        "amount": "金额",
        "tax_rate": "税率/征收率",
        "tax_amount": "税额",
    }.get(field, field)
