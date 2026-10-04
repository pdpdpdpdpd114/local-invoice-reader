import os
from datetime import date
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, QPoint
from PySide6.QtGui import QKeySequence, QPixmap
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from invoice_tool.applications import generate_application_number
from invoice_tool.models import InvoiceEntry, InvoiceItem, InvoiceRecord, PurchaseApplication
from invoice_tool.renaming import build_invoice_rename_plan, rename_invoice_files, undo_invoice_renames
from invoice_tool.ui import FolderSheet, MainWindow, PendingFolderMatch, unique_invoice_assignment


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _record(number: str) -> InvoiceRecord:
    return InvoiceRecord(
        invoice_number=number,
        total_with_tax=Decimal("10.10"),
        amount_total=Decimal("10.00"),
        tax_total=Decimal("0.10"),
        source_file=Path(f"{number}.pdf"),
        items=[
            InvoiceItem(
                project_name="测试项目",
                unit="个",
                quantity=Decimal("1"),
                unit_price=Decimal("10"),
                amount=Decimal("10"),
                tax_rate=Decimal("0.01"),
                tax_amount=Decimal("0.10"),
            )
        ],
    )


def _create_folder(window: MainWindow, group: str, selected_date: QDate):
    value = date(selected_date.year(), selected_date.month(), selected_date.day())
    return window._create_folder_with_values(value, group)


def _add_entry(window: MainWindow, folder, record: InvoiceRecord | None = None) -> InvoiceEntry:
    entry = InvoiceEntry(source_file=record.source_file if record else Path("waiting.pdf"), record=record)
    folder.entries.append(entry)
    window._add_entry_item(folder, entry)
    window._update_folder_item(folder)
    return entry


def test_files_require_selected_folder_and_keep_their_folder_defaults() -> None:
    _app()
    window = MainWindow()
    try:
        window.add_files([Path(__file__)])
        assert not window.folders

        first = _create_folder(window, "电控", QDate(2026, 8, 18))
        entry = _add_entry(window, first)
        second = _create_folder(window, "动力", QDate(2026, 8, 21))

        parsed = _record("00000000000000000001")
        window._receive_record(entry.entry_id, parsed)

        assert parsed.purchase_date == date(2026, 8, 18)
        assert parsed.group_name == "电控"
        assert window._selected_folder() is second
        assert len(first.entries) == 1
        assert len(second.entries) == 0
    finally:
        window.close()


def test_identical_date_and_group_are_separate_folders() -> None:
    _app()
    window = MainWindow()
    try:
        first = _create_folder(window, "悬架", QDate(2026, 8, 20))
        second = _create_folder(window, "悬架", QDate(2026, 8, 20))
        first_entry = _add_entry(window, first, _record("00000000000000000002"))
        second_entry = _add_entry(window, second, _record("00000000000000000003"))

        assert first.folder_id != second.folder_id
        assert [entry.entry_id for entry in first.entries] == [first_entry.entry_id]
        assert [entry.entry_id for entry in second.entries] == [second_entry.entry_id]
        assert [record.invoice_number for record in window._all_records()] == [
            "00000000000000000002",
            "00000000000000000003",
        ]
    finally:
        window.close()


def test_confirm_moves_within_folder_then_to_next_folder() -> None:
    app = _app()
    window = MainWindow()
    try:
        first_folder = _create_folder(window, "电控", QDate(2026, 8, 18))
        first = _add_entry(window, first_folder, _record("00000000000000000001"))
        second = _add_entry(window, first_folder, _record("00000000000000000002"))
        next_folder = _create_folder(window, "动力", QDate(2026, 8, 21))
        third = _add_entry(window, next_folder, _record("00000000000000000003"))

        window._select_entry(first.entry_id)
        window.confirm_current()
        app.processEvents()
        assert first.record and first.record.confirmed
        assert window._current_entry_id() == second.entry_id

        window.confirm_current()
        app.processEvents()
        assert second.record and second.record.confirmed
        assert window._current_entry_id() == third.entry_id
    finally:
        window.close()


def test_only_empty_folder_can_be_deleted_and_entry_removal_keeps_other_folder() -> None:
    _app()
    window = MainWindow()
    try:
        first = _create_folder(window, "制动", QDate(2026, 8, 22))
        entry = _add_entry(window, first, _record("00000000000000000004"))
        second = _create_folder(window, "空套", QDate(2026, 8, 23))

        window._select_entry(entry.entry_id)
        window.remove_selected()
        assert not first.entries
        assert second in window.folders

        window.batch_tree.setCurrentItem(window._folder_items[first.folder_id])
        window.delete_selected_folder()
        assert first not in window.folders
        assert second in window.folders
    finally:
        window.close()


def test_confirmed_entry_stays_confirmed_until_cancelled() -> None:
    _app()
    window = MainWindow()
    try:
        folder = _create_folder(window, "车架", QDate(2026, 8, 24))
        confirmed = _add_entry(window, folder, _record("00000000000000000005"))
        confirmed.record.confirmed = True
        other = _add_entry(window, folder, _record("00000000000000000006"))

        window._select_entry(confirmed.entry_id)
        assert window.confirmed.isChecked()
        assert window.invoice_number.isReadOnly()

        window._select_entry(other.entry_id)
        window._select_entry(confirmed.entry_id)
        window.mark_unconfirmed()
        assert confirmed.record and confirmed.record.confirmed

        window.cancel_confirmation()
        assert confirmed.record and not confirmed.record.confirmed
        assert not window.invoice_number.isReadOnly()
    finally:
        window.close()


def test_edit_folder_updates_inherited_values_but_preserves_invoice_exception() -> None:
    _app()
    window = MainWindow()
    try:
        folder = _create_folder(window, "电控", QDate(2026, 8, 18))
        inherited = _add_entry(window, folder, _record("00000000000000000007"))
        exception = _add_entry(window, folder, _record("00000000000000000008"))
        inherited.record.purchase_date = date(2026, 8, 18)
        inherited.record.group_name = "电控"
        exception.record.purchase_date = date(2026, 8, 19)
        exception.record.group_name = "动力"

        window.batch_tree.setCurrentItem(window._folder_items[folder.folder_id])
        window._update_folder_values(folder, date(2026, 8, 25), "悬架")

        assert folder.purchase_date == date(2026, 8, 25)
        assert folder.group_name == "悬架"
        assert inherited.record.purchase_date == date(2026, 8, 25)
        assert inherited.record.group_name == "悬架"
        assert exception.record.purchase_date == date(2026, 8, 19)
        assert exception.record.group_name == "动力"
    finally:
        window.close()


def test_shortcuts_are_bound_to_main_actions() -> None:
    _app()
    window = MainWindow()
    try:
        assert window.create_folder_action.shortcut().matches(QKeySequence("Ctrl+N")) == QKeySequence.SequenceMatch.ExactMatch
        assert window.import_bundle_action.shortcut().matches(QKeySequence("Ctrl+I")) == QKeySequence.SequenceMatch.ExactMatch
        assert window.add_action.shortcut().matches(QKeySequence("Ctrl+O")) == QKeySequence.SequenceMatch.ExactMatch
        assert window.confirm_action.shortcut().matches(QKeySequence("Ctrl+Return")) == QKeySequence.SequenceMatch.ExactMatch
        assert window.cancel_confirm_action.shortcut().matches(QKeySequence("Ctrl+Shift+Return")) == QKeySequence.SequenceMatch.ExactMatch
        assert window.edit_folder_action.shortcut().matches(QKeySequence("Ctrl+E")) == QKeySequence.SequenceMatch.ExactMatch
    finally:
        window.close()


def test_folder_label_shows_recognized_total_and_waiting_count() -> None:
    _app()
    window = MainWindow()
    try:
        folder = _create_folder(window, "外宣", QDate(2026, 8, 26))
        first = _add_entry(window, folder, _record("00000000000000000009"))
        second = _add_entry(window, folder, _record("00000000000000000010"))
        first.record.total_with_tax = Decimal("10.10")
        second.record.total_with_tax = Decimal("20.20")
        waiting = _add_entry(window, folder)
        window._update_folder_item(folder)

        text = window._folder_items[folder.folder_id].text(0)
        assert "¥30.30" in text
        assert "待识别 1" in text
        assert "已确认 0 张" in text
        assert waiting.entry_id in window._entry_items
    finally:
        window.close()


def test_folder_sheet_requires_group_and_returns_values() -> None:
    _app()
    sheet = FolderSheet(
        None,
        title="新建日期分组",
        purchase_date=date(2026, 8, 28),
    )
    try:
        sheet._accept_if_valid()
        assert sheet.result() == 0
        assert sheet.group_combo.property("invalid") is True

        sheet.group_combo.setCurrentText("电控")
        sheet._accept_if_valid()
        assert sheet.result() == 1
        assert sheet.values() == (date(2026, 8, 28), "电控")
    finally:
        sheet.close()


def test_toolbar_import_state_and_invoice_status_follow_selection() -> None:
    _app()
    window = MainWindow()
    try:
        assert not window.add_action.isEnabled()
        folder = _create_folder(window, "动力", QDate(2026, 8, 29))
        assert window.add_action.isEnabled()

        entry = _add_entry(window, folder, _record("00000000000000000011"))
        window._select_entry(entry.entry_id)
        assert window.invoice_status_badge.text() == "待复核"

        entry.record.confirmed = True
        window.show_entry(entry.entry_id)
        assert window.invoice_status_badge.text() == "✓ 已确认"
        assert window.cancel_confirm_button.isVisible() is False or window.cancel_confirm_button.isEnabled()
    finally:
        window.close()


def test_rename_paths_refresh_tree_and_image_preview_reference(tmp_path: Path) -> None:
    _app()
    window = MainWindow()
    try:
        source = tmp_path / "待整理.png"
        source.write_bytes(b"not-a-real-image")
        folder = _create_folder(window, "悬架", QDate(2026, 8, 29))
        record = _record("00000000000000000019")
        record.source_file = source
        record.preview_path = source
        record.group_name = "悬架"
        record.confirmed = True
        entry = _add_entry(window, folder, record)
        entry.source_file = source
        window._set_entry_text(entry.entry_id)
        window._update_batch_summary()

        plan = build_invoice_rename_plan(window.folders)
        assert window.rename_action.isEnabled()
        assert rename_invoice_files(plan.ready_items).success
        window._apply_invoice_rename_paths(plan.ready_items)

        renamed = tmp_path / "XJ-00000000000000000019.png"
        assert entry.source_file == renamed
        assert record.source_file == renamed
        assert record.preview_path == renamed
        assert renamed.name in window._entry_items[entry.entry_id].text(0)
        assert str(renamed) == window._entry_items[entry.entry_id].toolTip(0)

        assert undo_invoice_renames(plan.ready_items).success
        window._apply_invoice_rename_paths(plan.ready_items, undo=True)
        assert entry.source_file == source
        assert record.preview_path == source
    finally:
        window.close()


def test_folder_selection_shows_application_inspector_and_confirms() -> None:
    _app()
    window = MainWindow()
    try:
        application = PurchaseApplication(
            applicant="杨涛义",
            purchase_date=date(2026, 7, 29),
            group_name="车架",
            purchase_amount=Decimal("10.10"),
            application_number="CJ20260729",
        )
        folder = window._create_folder_with_values(application.purchase_date, application.group_name, application)
        entry = _add_entry(window, folder, _record("00000000000000000012"))
        entry.record.confirmed = True
        window.batch_tree.setCurrentItem(window._folder_items[folder.folder_id])
        assert window._displayed_folder_id == folder.folder_id
        assert window.application_number.text() == "CJ20260729"
        assert window.application_applicant.text() == "杨涛义"
        window.confirm_application_current()
        assert folder.application and folder.application.confirmed
    finally:
        window.close()


def test_automatic_application_number_recomputes_when_group_changes() -> None:
    _app()
    window = MainWindow()
    try:
        application = PurchaseApplication(
            applicant="杨涛义",
            purchase_date=date(2026, 7, 29),
            group_name="车架",
            purchase_amount=Decimal("10.10"),
            application_number="CJ20260729",
        )
        folder = window._create_folder_with_values(application.purchase_date, application.group_name, application)
        window.show_application(folder)
        window.application_group.setCurrentText("电控")
        window.save_application_current()
        assert folder.application and folder.application.application_number == "DK20260729"
    finally:
        window.close()


def test_export_ignores_empty_manual_folder(tmp_path: Path, monkeypatch) -> None:
    _app()
    window = MainWindow()
    try:
        # This mirrors a user creating an empty fallback folder before importing a real application bundle.
        window._create_folder_with_values(date(2026, 8, 19), "电控")
        application = PurchaseApplication(
            applicant="袁成龙",
            purchase_date=date(2026, 7, 15),
            group_name="电控",
            purchase_amount=Decimal("10.10"),
            application_number="DK20260715",
            confirmed=True,
        )
        folder = window._create_folder_with_values(application.purchase_date, application.group_name, application)
        entry = _add_entry(window, folder, _record("00000000000000000013"))
        entry.record.confirmed = True
        output = tmp_path / "purchase.xlsx"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *_args: (str(output), "Excel 文件 (*.xlsx)")))
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *_args: None))
        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *_args: None))

        window.export_current_batch()

        assert output.exists()
    finally:
        window.close()


def test_zoom_keeps_pointer_position_on_the_same_preview_region() -> None:
    app = _app()
    window = MainWindow()
    try:
        window.resize(1200, 800)
        window.show()
        app.processEvents()
        window._preview_source = QPixmap(1200, 900)
        window._rescale_preview()
        app.processEvents()

        image_origin = window.preview.mapTo(window.preview_scroll.viewport(), QPoint(0, 0))
        pointer = image_origin + QPoint(window.preview.width() // 3, window.preview.height() // 3)
        before = window.preview.mapFrom(window.preview_scroll.viewport(), pointer)
        before_ratio = (before.x() / window.preview.width(), before.y() / window.preview.height())
        window.zoom_at(1.25, pointer)
        app.processEvents()
        app.processEvents()
        after = window.preview.mapFrom(window.preview_scroll.viewport(), pointer)
        after_ratio = (after.x() / window.preview.width(), after.y() / window.preview.height())

        assert window._preview_zoom == 1.25
        assert abs(after_ratio[0] - before_ratio[0]) < 0.03
        assert abs(after_ratio[1] - before_ratio[1]) < 0.03
    finally:
        window.close()


def test_batch_folder_import_creates_one_application_per_valid_folder(tmp_path: Path, monkeypatch) -> None:
    _app()
    first_path = tmp_path / "B资料"
    second_path = tmp_path / "a资料"
    for folder in (first_path, second_path):
        folder.mkdir()
        (folder / "申请表.docx").write_bytes(b"application")
        (folder / "发票.pdf").write_bytes(b"invoice")
    (second_path / "子目录").mkdir()
    (second_path / "子目录" / "不应导入.pdf").write_bytes(b"nested invoice")

    window = MainWindow()
    queued: list[tuple[str, Path]] = []
    try:
        def parse_application(path: Path, existing: set[str]) -> PurchaseApplication:
            return PurchaseApplication(
                source_file=path,
                applicant="测试人",
                purchase_date=date(2026, 8, 24),
                group_name="电控",
                purchase_amount=Decimal("10.00"),
                application_number=generate_application_number("电控", date(2026, 8, 24), existing),
            )

        monkeypatch.setattr(window.service, "parse_application", parse_application)
        monkeypatch.setattr(window, "_enqueue_recognition", lambda jobs: queued.extend(jobs))

        imported, invoices, skipped = window.import_folders([first_path, second_path, first_path], show_summary=False)

        assert (imported, invoices) == (2, 2)
        assert len(skipped) == 1 and skipped[0][1] == "重复选择。"
        assert [folder.application.application_number for folder in window.folders] == ["DK20260824", "DK20260824-02"]
        assert [entry.source_file.name for folder in window.folders for entry in folder.entries] == ["发票.pdf", "发票.pdf"]
        assert len(queued) == 2

        imported, invoices, skipped = window.import_folders([second_path], show_summary=False)
        assert (imported, invoices) == (0, 0)
        assert skipped[0][1] == "已在当前批次导入。"
    finally:
        window.close()


def test_batch_import_keeps_valid_folder_when_another_folder_is_invalid(tmp_path: Path, monkeypatch) -> None:
    _app()
    valid = tmp_path / "有效"
    invalid = tmp_path / "无发票"
    valid.mkdir()
    invalid.mkdir()
    (valid / "申请表.docx").write_bytes(b"application")
    (valid / "发票.jpg").write_bytes(b"invoice")
    (invalid / "申请表.docx").write_bytes(b"application")

    window = MainWindow()
    try:
        monkeypatch.setattr(
            window.service,
            "parse_application",
            lambda path, _existing: PurchaseApplication(
                source_file=path,
                applicant="测试人",
                purchase_date=date(2026, 8, 24),
                group_name="电控",
                purchase_amount=Decimal("10.00"),
                application_number="DK20260824",
            ),
        )
        monkeypatch.setattr(window, "_enqueue_recognition", lambda _jobs: None)

        imported, invoices, skipped = window.import_folders([invalid, valid], show_summary=False)

        assert (imported, invoices) == (1, 1)
        assert skipped == [(invalid, "未找到 PDF、JPG、JPEG 或 PNG 发票。")]
        assert len(window.folders) == 1
    finally:
        window.close()


def test_unique_invoice_assignment_requires_one_global_partition() -> None:
    assignment = unique_invoice_assignment(
        [Decimal("10.00"), Decimal("5.00")],
        [Decimal("10.00"), Decimal("2.00"), Decimal("3.00")],
    )
    assert assignment == [1, 6]
    assert unique_invoice_assignment(
        [Decimal("10.00"), Decimal("10.00")], [Decimal("10.00"), Decimal("10.00")]
    ) is None
    assert unique_invoice_assignment([Decimal("11.00")], [Decimal("10.00")]) is None
    assert unique_invoice_assignment([Decimal("21.00")], [Decimal("1.00")] * 21) is None


def test_batch_folder_import_keeps_multiple_applications_for_later_matching(tmp_path: Path, monkeypatch) -> None:
    _app()
    source = tmp_path / "多申请表"
    source.mkdir()
    for name in ("申请表A.docx", "申请表B.docx"):
        (source / name).write_bytes(b"application")
    (source / "发票.pdf").write_bytes(b"invoice")
    window = MainWindow()
    queued: list[tuple[str, Path]] = []
    try:
        def parse_application(path: Path, existing: set[str]) -> PurchaseApplication:
            return PurchaseApplication(
                source_file=path,
                applicant=path.stem[-1],
                purchase_date=date(2026, 8, 24),
                group_name="电控",
                purchase_amount=Decimal("10.00"),
                application_number=generate_application_number("电控", date(2026, 8, 24), existing),
            )

        monkeypatch.setattr(window.service, "parse_application", parse_application)
        monkeypatch.setattr(window, "_enqueue_recognition", lambda jobs: queued.extend(jobs))
        imported, invoices, skipped = window.import_folders([source], show_summary=False)

        assert (imported, invoices, skipped) == (1, 1, [])
        applications = [folder for folder in window.folders if folder.application]
        pending = [folder for folder in window.folders if folder.is_unassigned]
        assert [folder.application.application_number for folder in applications] == ["DK20260824", "DK20260824-02"]
        assert len(pending) == 1 and len(pending[0].entries) == 1
        assert len(queued) == 1
    finally:
        window.close()


def test_automatic_assignment_moves_unique_matches_and_manual_assignment_moves_pending(monkeypatch, tmp_path: Path) -> None:
    _app()
    window = MainWindow()
    source = tmp_path / "多申请表"
    source.mkdir()
    try:
        first_application = PurchaseApplication(
            source_file=source / "申请表A.docx", applicant="甲", purchase_date=date(2026, 8, 24),
            group_name="电控", purchase_amount=Decimal("10.00"), application_number="DK20260824",
        )
        second_application = PurchaseApplication(
            source_file=source / "申请表B.docx", applicant="乙", purchase_date=date(2026, 8, 24),
            group_name="电控", purchase_amount=Decimal("5.00"), application_number="DK20260824-02",
        )
        first = window._create_folder_with_values(date(2026, 8, 24), "电控", first_application)
        second = window._create_folder_with_values(date(2026, 8, 24), "电控", second_application)
        unassigned = window._create_unassigned_folder(source)
        entries = []
        for number, total in (("00000000000000000014", "10.00"), ("00000000000000000015", "2.00"), ("00000000000000000016", "3.00")):
            entry = InvoiceEntry(source_file=source / f"{number}.pdf", record=_record(number))
            entry.record.total_with_tax = Decimal(total)
            unassigned.entries.append(entry)
            window._add_entry_item(unassigned, entry)
            entries.append(entry)
        window._pending_folder_matches[window._path_key(source)] = PendingFolderMatch(
            source_folder=source,
            application_folder_ids=[first.folder_id, second.folder_id],
            unassigned_folder_id=unassigned.folder_id,
            entry_ids=[entry.entry_id for entry in entries],
        )
        monkeypatch.setattr(QMessageBox, "information", staticmethod(lambda *_args: None))

        window._finalize_pending_matches()

        assert [entry.source_file.name for entry in first.entries] == [entries[0].source_file.name]
        assert [entry.source_file.name for entry in second.entries] == [entries[1].source_file.name, entries[2].source_file.name]
        assert unassigned not in window.folders

        pending = window._create_unassigned_folder(source)
        manual = InvoiceEntry(source_file=source / "待分配.pdf", record=_record("00000000000000000017"))
        pending.entries.append(manual)
        window._add_entry_item(pending, manual)
        window._select_entry(manual.entry_id)
        window.assign_selected_entry(first.folder_id)
        assert manual in first.entries and manual not in pending.entries
        assert first.application and not first.application.confirmed
    finally:
        window.close()


def test_export_blocks_unassigned_invoices(monkeypatch, tmp_path: Path) -> None:
    _app()
    source = tmp_path / "待分配"
    source.mkdir()
    window = MainWindow()
    messages: list[str] = []
    try:
        pending = window._create_unassigned_folder(source)
        entry = InvoiceEntry(source_file=source / "发票.pdf", record=_record("00000000000000000018"))
        entry.record.confirmed = True
        pending.entries.append(entry)
        window._add_entry_item(pending, entry)
        monkeypatch.setattr(QMessageBox, "warning", staticmethod(lambda *_args: messages.append(_args[-1])))

        window.export_current_batch()

        assert messages == ["仍有 1 张待分配发票，请先右键分配到申请单。"]
    finally:
        window.close()
