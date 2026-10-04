from decimal import Decimal

from invoice_tool.models import InvoiceItem, InvoiceRecord
from invoice_tool.validation import validate_duplicates, validate_invoice


def valid_invoice(number: str = "00000000000000000024") -> InvoiceRecord:
    return InvoiceRecord(
        invoice_number=number,
        total_with_tax=Decimal("28.61"),
        amount_total=Decimal("28.33"),
        tax_total=Decimal("0.28"),
        items=[
            InvoiceItem("*电子元件*原装正品", "规格", "件", Decimal("4"), Decimal("1.8217821782178"), Decimal("7.29"), Decimal("0.01"), Decimal("0.07")),
            InvoiceItem("*电子元件*原装正品", "规格", "件", Decimal("3"), Decimal("3.6369636963366"), Decimal("10.91"), Decimal("0.01"), Decimal("0.11")),
            InvoiceItem("*电子元件*原装正品", "规格", "件", Decimal("3"), Decimal("2.0693069306931"), Decimal("6.21"), Decimal("0.01"), Decimal("0.06")),
            InvoiceItem("*电子元件*原装正品", "规格", "件", Decimal("1"), Decimal("1.7425742574257"), Decimal("1.74"), Decimal("0.01"), Decimal("0.02")),
            InvoiceItem("*电子元件*AMS1117-1.2", "规格", "件", Decimal("4"), Decimal("0.5445544554455"), Decimal("2.18"), Decimal("0.01"), Decimal("0.02")),
        ],
    )


def test_valid_invoice_passes() -> None:
    assert validate_invoice(valid_invoice()).valid


def test_mismatched_tax_is_rejected() -> None:
    invoice = valid_invoice()
    invoice.items[0].tax_amount = Decimal("0.99")
    assert any(issue.field == "tax_amount" for issue in validate_invoice(invoice).errors)


def test_duplicate_invoice_numbers_are_rejected() -> None:
    assert len(validate_duplicates([valid_invoice(), valid_invoice()])) == 2
