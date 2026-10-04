from datetime import date
from decimal import Decimal
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from invoice_tool.applications import generate_application_number, parse_application_docx
from invoice_tool.models import InvoiceItem, InvoiceRecord, PurchaseApplication
from invoice_tool.service import scan_import_folder
from invoice_tool.validation import reconcile_application, validate_application


def _docx(path: Path, rows: list[list[str]]) -> Path:
    cells = "".join(
        "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{value}</w:t></w:r></w:p></w:tc>" for value in row) + "</w:tr>"
        for row in rows
    )
    xml = f'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:tbl>{cells}</w:tbl></w:body></w:document>'
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml)
        archive.writestr("[Content_Types].xml", "<Types xmlns=\"http://schemas.openxmlformats.org/package/2006/content-types\" />")
    return path


def test_application_docx_reads_labels_and_generates_number(tmp_path: Path) -> None:
    path = _docx(tmp_path / "申请表.docx", [["部 门", "车架组", "申 请 人", "杨涛义"], ["金 额", "268.21元", "采购时间", "2026.7.29"]])
    application = parse_application_docx(path)
    assert application.group_name == "车架"
    assert application.applicant == "杨涛义"
    assert application.purchase_date == date(2026, 7, 29)
    assert application.purchase_amount == Decimal("268.21")
    assert application.application_number == "CJ20260729"


def test_application_number_suffixes_and_unknown_group() -> None:
    current = {"DK20260729", "DK20260729-02"}
    assert generate_application_number("电控组", date(2026, 7, 29), current) == "DK20260729-03"
    assert generate_application_number("未知组", date(2026, 7, 29), set()) == ""


def test_reconciliation_requires_explicit_acknowledgement() -> None:
    application = PurchaseApplication(
        applicant="杨涛义", group_name="车架", purchase_date=date(2026, 7, 29),
        purchase_amount=Decimal("10.00"), application_number="CJ20260729",
    )
    invoice = InvoiceRecord(total_with_tax=Decimal("10.02"), items=[InvoiceItem()])
    result = reconcile_application(application, [invoice])
    assert result.difference == Decimal("0.02") and not result.matched
    assert not validate_application(application, [invoice]).valid
    application.difference_acknowledged = True
    assert validate_application(application, [invoice]).valid


def test_folder_scan_uses_only_direct_visible_supported_files(tmp_path: Path) -> None:
    folder = tmp_path / "资料"
    folder.mkdir()
    application = _docx(folder / "申请表.docx", [["部门", "电控"]])
    invoice = folder / "发票.PDF"
    invoice.write_text("not parsed here", encoding="utf-8")
    _docx(folder / "~$申请表.docx", [["部门", "忽略"]])
    (folder / ".hidden.pdf").write_text("ignored", encoding="utf-8")
    (folder / "说明.txt").write_text("ignored", encoding="utf-8")
    child = folder / "子文件夹"
    child.mkdir()
    _docx(child / "子申请表.docx", [["部门", "忽略"]])
    (child / "子发票.pdf").write_text("ignored", encoding="utf-8")

    result = scan_import_folder(folder)

    assert result.valid
    assert result.application_files == [application]
    assert result.invoice_files == [invoice]


def test_folder_scan_reports_missing_documents_and_keeps_all_applications(tmp_path: Path) -> None:
    missing_application = tmp_path / "缺申请表"
    missing_application.mkdir()
    (missing_application / "发票.pdf").write_text("invoice", encoding="utf-8")
    assert scan_import_folder(missing_application).error == "未找到 DOCX 申请表。"

    ambiguous = tmp_path / "多申请表"
    ambiguous.mkdir()
    _docx(ambiguous / "申请表1.docx", [["部门", "电控"]])
    _docx(ambiguous / "申请表2.docx", [["部门", "动力"]])
    (ambiguous / "发票.png").write_bytes(b"invoice")
    result = scan_import_folder(ambiguous)
    assert result.valid
    assert [path.name for path in result.application_files] == ["申请表1.docx", "申请表2.docx"]
