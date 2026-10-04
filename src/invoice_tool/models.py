from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal
from uuid import uuid4


FieldName = Literal[
    "project_name",
    "specification",
    "unit",
    "quantity",
    "unit_price",
    "amount",
    "tax_rate",
    "tax_amount",
    "invoice_number",
    "total_with_tax",
]


@dataclass(slots=True)
class OcrToken:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    confidence: float = 1.0
    page: int = 1

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2


@dataclass(slots=True)
class InvoiceItem:
    project_name: str = ""
    specification: str = ""
    unit: str = ""
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    amount: Decimal | None = None
    tax_rate: Decimal | None = None
    tax_amount: Decimal | None = None
    confidence: dict[str, float] = field(default_factory=dict)
    boxes: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)


@dataclass(slots=True)
class InvoiceRecord:
    invoice_number: str = ""
    total_with_tax: Decimal | None = None
    source_file: Path = Path()
    items: list[InvoiceItem] = field(default_factory=list)
    confirmed: bool = False
    amount_total: Decimal | None = None
    tax_total: Decimal | None = None
    route: str = ""
    preview_path: Path | None = None
    parse_message: str = ""
    purchase_date: date | None = None
    group_name: str = ""


@dataclass(slots=True)
class PurchaseApplication:
    """The locally parsed Word purchase application that owns one or more invoices."""

    source_file: Path | None = None
    applicant: str = ""
    purchase_date: date | None = None
    group_name: str = ""
    purchase_amount: Decimal | None = None
    application_number: str = ""
    number_is_automatic: bool = True
    payment_date: date | None = None
    confirmed: bool = False
    difference_acknowledged: bool = False
    parse_message: str = ""


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    application_amount: Decimal | None
    invoice_total: Decimal
    difference: Decimal | None

    @property
    def matched(self) -> bool:
        return self.difference is not None and abs(self.difference) <= Decimal("0.01")


@dataclass(slots=True)
class InvoiceEntry:
    """One source file inside a user-created batch folder."""

    entry_id: str = field(default_factory=lambda: uuid4().hex)
    source_file: Path = Path()
    record: InvoiceRecord | None = None
    error: str = ""


@dataclass(slots=True)
class InvoiceFolder:
    """A virtual folder in the current batch; source files stay in place."""

    folder_id: str = field(default_factory=lambda: uuid4().hex)
    purchase_date: date = field(default_factory=date.today)
    group_name: str = ""
    creation_order: int = 0
    entries: list[InvoiceEntry] = field(default_factory=list)
    application: PurchaseApplication | None = None
    source_folder: Path | None = None
    is_unassigned: bool = False


@dataclass(slots=True)
class ValidationIssue:
    field: str
    message: str
    severity: Literal["error", "warning"] = "error"
    item_index: int | None = None


@dataclass(slots=True)
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]
