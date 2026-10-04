from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from .models import InvoiceItem, InvoiceRecord, OcrToken
from .validation import to_decimal

NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")
INVOICE_RE = re.compile(r"发票号码\s*[：:]?\s*(\d{15,25})")


class UnsupportedInvoiceError(ValueError):
    """输入文件不是第一版支持的电子普通发票商品明细版式。"""


def parse_invoice_tokens(
    tokens: list[OcrToken],
    page_width: float,
    page_height: float,
    source_file: Path,
    route: str,
) -> InvoiceRecord:
    if not tokens:
        raise UnsupportedInvoiceError("未读取到文字内容。")
    lines = _group_lines(tokens, page_height)
    all_text = "\n".join(_line_text(line) for line in lines)
    # Text-layer PDFs often split a header into separate glyph tokens (for
    # example, "税 率/征收率").  Layout parsing below still uses coordinates;
    # this compact form is only used to recognize the supported template.
    compact_text = re.sub(r"\s+", "", all_text)
    # Image OCR can split or omit a character in the decorative title (for
    # example, "电子发" rather than "电子发票").  The item-table headers and
    # later total/invoice-number checks are the reliable template evidence.
    if "项目名称" not in compact_text or "税率" not in compact_text:
        raise UnsupportedInvoiceError("仅支持包含标准商品明细表的电子普通发票。")

    invoice_number = _extract_invoice_number(compact_text)
    header_index = _find_header_line(lines)
    total_index = _find_total_line(lines, header_index)
    if header_index is None or total_index is None:
        raise UnsupportedInvoiceError("未定位到商品明细表或合计区域。")

    item_starts = [
        index
        for index in range(header_index + 1, total_index)
        if _is_item_start(lines[index], page_width)
    ]
    if not item_starts:
        raise UnsupportedInvoiceError("未识别到可核对的商品明细行。")

    columns = _column_boundaries(lines[header_index], page_width)

    items: list[InvoiceItem] = []
    for position, start in enumerate(item_starts):
        end = item_starts[position + 1] if position + 1 < len(item_starts) else total_index
        item = _parse_item(lines[start], lines[start + 1 : end], page_width, columns)
        items.append(item)

    amount_total, tax_total = _parse_subtotals(lines[total_index], page_width)
    total_with_tax = _parse_total_with_tax(lines, total_index, page_width)
    if not invoice_number or total_with_tax is None:
        raise UnsupportedInvoiceError("未可靠识别发票号码或价税合计。")
    return InvoiceRecord(
        invoice_number=invoice_number,
        total_with_tax=total_with_tax,
        source_file=source_file,
        items=items,
        amount_total=amount_total,
        tax_total=tax_total,
        route=route,
    )


def _group_lines(tokens: list[OcrToken], page_height: float) -> list[list[OcrToken]]:
    tolerance = max(4.0, page_height * 0.009)
    groups: list[list[OcrToken]] = []
    for token in sorted(tokens, key=lambda value: (value.cy, value.x0)):
        if groups and abs(token.cy - _line_y(groups[-1])) <= tolerance:
            groups[-1].append(token)
        else:
            groups.append([token])
    return [sorted(group, key=lambda value: value.x0) for group in groups]


def _line_y(line: list[OcrToken]) -> float:
    return sum(token.cy for token in line) / len(line)


def _line_text(line: list[OcrToken]) -> str:
    return " ".join(token.text.strip() for token in line if token.text.strip())


def _extract_invoice_number(all_text: str) -> str:
    matched = INVOICE_RE.search(all_text)
    if matched:
        return matched.group(1)
    candidates = re.findall(r"\b\d{20}\b", all_text)
    return candidates[0] if candidates else ""


def _find_header_line(lines: list[list[OcrToken]]) -> int | None:
    labels = ("项目名称", "规格型号", "单位", "数量", "单价", "金额", "税率", "税额")
    best_index: int | None = None
    best_score = 0
    for index, line in enumerate(lines):
        text = _line_text(line).replace(" ", "")
        score = sum(1 for label in labels if label in text)
        if score > best_score:
            best_index, best_score = index, score
    return best_index if best_score >= 5 else None


def _find_total_line(lines: list[list[OcrToken]], header_index: int | None) -> int | None:
    if header_index is None:
        return None
    for index in range(header_index + 1, len(lines)):
        text = _line_text(lines[index]).replace(" ", "")
        if "价税合计" in text:
            return index - 1 if index > header_index + 1 else None
    for index in range(header_index + 1, len(lines)):
        text = _line_text(lines[index]).replace(" ", "")
        if text.startswith("合计") or "合计" in text:
            return index
    return None


def _is_item_start(line: list[OcrToken], width: float) -> bool:
    text = _line_text(line)
    if "%" not in text:
        return False
    numeric_parts = _right_side_numbers(line, width)
    return len(numeric_parts) >= 4 and any("%" in token.text for token in line)


def _right_side_numbers(line: list[OcrToken], width: float) -> list[tuple[Decimal, OcrToken]]:
    values: list[tuple[Decimal, OcrToken]] = []
    for token in line:
        if token.cx < width * 0.40 or "%" in token.text:
            continue
        cleaned = token.text.replace("¥", "").replace("￥", "").replace(",", "")
        for value in NUMBER_RE.findall(cleaned):
            parsed = to_decimal(value)
            if parsed is not None:
                values.append((parsed, token))
    return values


def _column_boundaries(header: list[OcrToken], width: float) -> tuple[float, float, float]:
    """Return the project/spec, spec/unit and unit/number column boundaries.

    Standard electronic invoices place the first three headers consistently,
    but PDF producers split "单位" into individual glyphs.  Deriving the
    boundaries from the visible headers avoids treating all wrapped text as a
    specification just because it is on a following line.
    """
    project = next((token for token in header if "项目名称" in token.text), None)
    specification = next((token for token in header if "规格型号" in token.text), None)
    unit_candidates = [
        token
        for token in header
        if token.cx > (specification.cx if specification else width * 0.20)
        and token.text.strip() in {"单", "位", "单位"}
    ]
    unit_center = sum(token.cx for token in unit_candidates[:2]) / len(unit_candidates[:2]) if unit_candidates else width * 0.34
    project_center = project.cx if project else width * 0.11
    specification_center = specification.cx if specification else width * 0.23
    return (
        (project_center + specification_center) / 2,
        (specification_center + unit_center) / 2,
        unit_center + (width * 0.06),
    )


def _parse_item(
    start: list[OcrToken],
    continuations: list[list[OcrToken]],
    width: float,
    columns: tuple[float, float, float],
) -> InvoiceItem:
    numeric_parts = _right_side_numbers(start, width)
    quantity, quantity_token = numeric_parts[0]
    unit_price, unit_price_token = numeric_parts[1]
    amount, amount_token = numeric_parts[2]
    tax_amount, tax_token = numeric_parts[-1]
    rate_token = next((token for token in start if "%" in token.text), None)
    tax_rate = to_decimal(rate_token.text if rate_token else None)

    project_end, specification_end, unit_end = columns
    # Use the token's left edge for the first boundary.  A wrapped word can
    # slightly cross the visual divider (for example "贴" in "贴片"), while
    # its left edge still belongs to the project-name column.
    project_tokens = [token for token in start if token.x0 < project_end]
    specification_tokens = [token for token in start if project_end <= token.x0 < specification_end]
    unit_tokens = [
        token
        for token in start
        if specification_end <= token.x0 < unit_end and not NUMBER_RE.fullmatch(token.text.replace(" ", ""))
    ]
    project_name = _join_tokens(project_tokens)
    unit = _join_tokens(unit_tokens)
    specification = _join_tokens(specification_tokens)
    for line in continuations:
        project_part = _join_tokens([token for token in line if token.x0 < project_end])
        specification_part = _join_tokens(
            [token for token in line if project_end <= token.x0 < specification_end]
        )
        # A visual line break inside either column is only a wrap, not a new
        # field.  Concatenating it keeps words such as "传感" + "器" and
        # "CHVS-" + "ASV-600" intact.
        project_name = _append_wrapped_text(project_name, project_part)
        specification = _append_wrapped_text(specification, specification_part)
    if project_name.endswith("贴") and specification.startswith("片"):
        project_name = project_name[:-1]
        specification = "贴" + specification

    return InvoiceItem(
        project_name=project_name,
        specification=specification,
        unit=unit,
        quantity=quantity,
        unit_price=unit_price,
        amount=amount,
        tax_rate=tax_rate,
        tax_amount=tax_amount,
        confidence={
            "project_name": _average_confidence(
                project_tokens + [token for line in continuations for token in line if token.x0 < project_end]
            ),
            "specification": _average_confidence(
                specification_tokens
                + [token for line in continuations for token in line if project_end <= token.x0 < specification_end]
            ),
            "unit": _average_confidence(unit_tokens),
            "quantity": quantity_token.confidence,
            "unit_price": unit_price_token.confidence,
            "amount": amount_token.confidence,
            "tax_rate": rate_token.confidence if rate_token else 0.0,
            "tax_amount": tax_token.confidence,
        },
    )


def _join_tokens(tokens: list[OcrToken]) -> str:
    return " ".join(token.text.strip() for token in tokens if token.text.strip()).strip()


def _append_wrapped_text(current: str, wrapped_part: str) -> str:
    if not wrapped_part:
        return current
    if not current:
        return wrapped_part
    return current + wrapped_part


def _average_confidence(tokens: list[OcrToken]) -> float:
    return sum(token.confidence for token in tokens) / len(tokens) if tokens else 1.0


def _parse_subtotals(line: list[OcrToken], width: float) -> tuple[Decimal | None, Decimal | None]:
    values = _right_side_numbers(line, width)
    if len(values) < 2:
        return None, None
    return values[0][0], values[-1][0]


def _parse_total_with_tax(lines: list[list[OcrToken]], total_index: int, width: float) -> Decimal | None:
    for line in lines[total_index + 1 : min(total_index + 5, len(lines))]:
        text = _line_text(line).replace(" ", "")
        if "小写" in text:
            values = _right_side_numbers(line, width * 0.65)
            if values:
                return values[-1][0]
            direct = NUMBER_RE.findall(text.replace("¥", "").replace("￥", ""))
            return to_decimal(direct[-1]) if direct else None
    return None
