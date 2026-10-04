"""Offline DOCX purchase-application extraction and numbering helpers."""

from __future__ import annotations

import re
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.etree import ElementTree as ET

from .models import PurchaseApplication

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": WORD_NS}
GROUP_PREFIXES = {
    "车架": "CJ",
    "电控": "DK",
    "悬架": "XJ",
    "制动": "ZD",
    "空套": "KT",
    "动力": "DL",
    "外宣": "WX",
}


class UnsupportedApplicationError(ValueError):
    """The file is not a supported local DOCX purchase application."""


def normalize_label(value: str) -> str:
    return re.sub(r"[\s：:()（）]+", "", value or "")


def normalize_group(value: str) -> str:
    group = normalize_label(value)
    return group[:-1] if group.endswith("组") else group


def parse_purchase_date(value: str) -> date | None:
    match = re.search(r"(\d{4})\s*(?:[.\-/年])\s*(\d{1,2})\s*(?:[.\-/月])\s*(\d{1,2})", value)
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def parse_money(value: str) -> Decimal | None:
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d+)?", value.replace("￥", "").replace("¥", ""))
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", ""))
    except InvalidOperation:
        return None


def generate_application_number(group_name: str, purchase_date: date | None, existing_numbers: set[str]) -> str:
    prefix = GROUP_PREFIXES.get(normalize_group(group_name), "")
    if not prefix or purchase_date is None:
        return ""
    base = f"{prefix}{purchase_date:%Y%m%d}"
    if base not in existing_numbers:
        return base
    suffix = 2
    while f"{base}-{suffix:02d}" in existing_numbers:
        suffix += 1
    return f"{base}-{suffix:02d}"


def parse_application_docx(source_file: str | Path, existing_numbers: set[str] | None = None) -> PurchaseApplication:
    path = Path(source_file)
    if path.suffix.lower() != ".docx":
        raise UnsupportedApplicationError("申请表仅支持 DOCX 格式。")
    if not path.exists():
        raise FileNotFoundError(path)
    try:
        with zipfile.ZipFile(path) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
    except (KeyError, zipfile.BadZipFile, ET.ParseError) as error:
        raise UnsupportedApplicationError("无法读取 Word 申请表。") from error

    values = _extract_labeled_values(root)
    group_name = normalize_group(values.get("部门", ""))
    purchase_date = parse_purchase_date(values.get("采购时间", ""))
    amount = parse_money(values.get("金额", ""))
    number = generate_application_number(group_name, purchase_date, existing_numbers or set())
    application = PurchaseApplication(
        source_file=path,
        applicant=values.get("申请人", "").strip(),
        purchase_date=purchase_date,
        group_name=group_name,
        purchase_amount=amount,
        application_number=number,
    )
    missing = []
    if not application.group_name:
        missing.append("部门")
    if not application.applicant:
        missing.append("申请人")
    if application.purchase_date is None:
        missing.append("采购时间")
    if application.purchase_amount is None:
        missing.append("金额")
    if missing:
        application.parse_message = f"未读取到：{'、'.join(missing)}。请在申请单检查器中补充。"
    return application


def _extract_labeled_values(root: ET.Element) -> dict[str, str]:
    wanted = {"部门", "申请人", "采购时间", "金额"}
    found: dict[str, str] = {}
    for row in root.findall(".//w:tr", NS):
        cells = [_cell_text(cell) for cell in row.findall("./w:tc", NS)]
        for index, cell in enumerate(cells):
            label = normalize_label(cell)
            for target in wanted - found.keys():
                if label == target and index + 1 < len(cells):
                    candidate = cells[index + 1].strip()
                    if candidate:
                        found[target] = candidate
                elif label.startswith(target) and len(label) > len(target):
                    candidate = cell.split("：", 1)[-1].split(":", 1)[-1].strip()
                    if candidate and normalize_label(candidate) != target:
                        found[target] = candidate
    return found


def _cell_text(cell: ET.Element) -> str:
    return "".join(node.text or "" for node in cell.findall(".//w:t", NS)).strip()
