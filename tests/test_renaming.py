from __future__ import annotations

from pathlib import Path

import pytest

from invoice_tool.models import InvoiceEntry, InvoiceFolder, InvoiceRecord
from invoice_tool.renaming import build_invoice_rename_plan, rename_invoice_files, undo_invoice_renames


def _folder(
    tmp_path: Path,
    *,
    filename: str,
    number: str,
    group: str,
    confirmed: bool = True,
    unassigned: bool = False,
) -> InvoiceFolder:
    source = tmp_path / filename
    source.write_bytes(b"invoice")
    record = InvoiceRecord(invoice_number=number, source_file=source, group_name=group, confirmed=confirmed)
    return InvoiceFolder(group_name=group, entries=[InvoiceEntry(source_file=source, record=record)], is_unassigned=unassigned)


@pytest.mark.parametrize(
    ("group", "prefix"),
    [
        ("车架", "CJ"), ("电控", "DK"), ("悬架", "XJ"), ("制动", "ZD"),
        ("空套", "KT"), ("动力", "DL"), ("外宣", "WX"),
    ],
)
def test_plan_uses_shared_uppercase_group_prefixes(tmp_path: Path, group: str, prefix: str) -> None:
    folder = _folder(tmp_path, filename="原票.JPEG", number="00000000000000000019", group=group)

    plan = build_invoice_rename_plan([folder])

    item = plan.ready_items[0]
    assert item.target is not None
    assert item.target.name == f"{prefix}-00000000000000000019.JPEG"


def test_plan_shows_skipped_and_conflict_entries_without_changing_files(tmp_path: Path) -> None:
    ready = _folder(tmp_path, filename="待改.pdf", number="00000000000000000019", group="悬架")
    pending = _folder(tmp_path, filename="未确认.png", number="00000000000000000020", group="制动", confirmed=False)
    unassigned = _folder(tmp_path, filename="待分配.jpg", number="00000000000000000021", group="车架", unassigned=True)
    collision = tmp_path / "XJ-00000000000000000019.pdf"
    collision.write_bytes(b"occupied")

    plan = build_invoice_rename_plan([ready, pending, unassigned])

    assert [item.status for item in plan.items] == ["conflict", "skipped", "skipped"]
    assert collision.read_bytes() == b"occupied"
    assert (tmp_path / "待改.pdf").exists()


def test_rename_and_undo_allow_target_that_is_another_planned_source(tmp_path: Path) -> None:
    first = _folder(tmp_path, filename="原票.pdf", number="00000000000000000019", group="悬架")
    second = _folder(tmp_path, filename="XJ-00000000000000000019.pdf", number="00000000000000000020", group="制动")
    plan = build_invoice_rename_plan([first, second])

    assert len(plan.ready_items) == 2
    result = rename_invoice_files(plan.ready_items)

    assert result.success
    assert (tmp_path / "XJ-00000000000000000019.pdf").exists()
    assert (tmp_path / "ZD-00000000000000000020.pdf").exists()
    undo = undo_invoice_renames(plan.ready_items)
    assert undo.success
    assert (tmp_path / "原票.pdf").exists()
    assert (tmp_path / "XJ-00000000000000000019.pdf").exists()


def test_undo_does_not_overwrite_file_created_after_rename(tmp_path: Path) -> None:
    folder = _folder(tmp_path, filename="原票.pdf", number="00000000000000000019", group="悬架")
    plan = build_invoice_rename_plan([folder])
    item = plan.ready_items[0]

    assert rename_invoice_files(plan.ready_items).success
    (tmp_path / "原票.pdf").write_bytes(b"new file")
    undo = undo_invoice_renames(plan.ready_items)

    assert not undo.success
    assert (tmp_path / "XJ-00000000000000000019.pdf").exists()
    assert (tmp_path / "原票.pdf").read_bytes() == b"new file"
