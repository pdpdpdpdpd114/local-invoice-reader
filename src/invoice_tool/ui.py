from __future__ import annotations

import ctypes
import sys
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

from PySide6.QtCore import QDate, QDir, QPoint, QRectF, QSize, QStandardPaths, QThread, QTimer, Qt, Signal, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QKeySequence, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFileSystemModel,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .applications import generate_application_number
from .excel_export import default_output_name, export_excel
from .models import InvoiceEntry, InvoiceFolder, InvoiceItem, InvoiceRecord, PurchaseApplication
from .renaming import InvoiceRenameItem, build_invoice_rename_plan, path_key, rename_invoice_files, undo_invoice_renames
from .service import InvoiceService, SUPPORTED_APPLICATION_EXTENSIONS, SUPPORTED_EXTENSIONS, scan_import_folder
from .validation import reconcile_application, to_decimal, validate_application, validate_duplicates, validate_invoice

ITEM_HEADERS = ["项目名称", "规格型号", "单位", "数量", "单价", "金额", "税率/征收率", "税额"]
ITEM_FIELDS = ["project_name", "specification", "unit", "quantity", "unit_price", "amount", "tax_rate", "tax_amount"]
GROUP_OPTIONS = ["请选择分组", "车架", "电控", "悬架", "制动", "空套", "动力", "外宣"]
MAX_AUTO_MATCH_INVOICES = 20
MAX_MATCH_CANDIDATES = 4096


@dataclass(slots=True)
class PendingFolderMatch:
    source_folder: Path
    application_folder_ids: list[str]
    unassigned_folder_id: str
    entry_ids: list[str]


def unique_invoice_assignment(
    application_amounts: list[Decimal | None], invoice_totals: list[Decimal | None]
) -> list[int] | None:
    """Return one disjoint invoice-mask per application, only when globally unique."""
    if (
        not application_amounts
        or not invoice_totals
        or len(invoice_totals) > MAX_AUTO_MATCH_INVOICES
        or any(amount is None for amount in application_amounts)
        or any(total is None for total in invoice_totals)
    ):
        return None

    def cents(value: Decimal) -> int:
        return int((value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) * 100).to_integral_value())

    values = [cents(total) for total in invoice_totals if total is not None]
    targets = [cents(amount) for amount in application_amounts if amount is not None]
    limit = 1 << len(values)
    sums = [0] * limit
    for mask in range(1, limit):
        low_bit = mask & -mask
        sums[mask] = sums[mask ^ low_bit] + values[low_bit.bit_length() - 1]

    candidates: list[list[int]] = []
    for target in targets:
        matched = [mask for mask in range(1, limit) if abs(sums[mask] - target) <= 1]
        if not matched or len(matched) > MAX_MATCH_CANDIDATES:
            return None
        candidates.append(matched)

    order = sorted(range(len(targets)), key=lambda index: len(candidates[index]))
    solutions: list[list[int]] = []

    def search(position: int, used: int, assigned: list[int]) -> None:
        if len(solutions) > 1:
            return
        if position == len(order):
            solutions.append(assigned.copy())
            return
        application_index = order[position]
        for mask in candidates[application_index]:
            if mask & used:
                continue
            assigned[application_index] = mask
            search(position + 1, used | mask, assigned)
            if len(solutions) > 1:
                return
        assigned[application_index] = 0

    search(0, 0, [0] * len(targets))
    return solutions[0] if len(solutions) == 1 else None


class FolderSheet(QDialog):
    """A compact, modal sheet for creating or editing a virtual batch folder."""

    def __init__(
        self,
        parent: QWidget,
        *,
        title: str,
        purchase_date: date,
        group_name: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("folderSheet")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(16)
        heading = QLabel(title)
        heading.setObjectName("sheetTitle")
        helper = QLabel("日期和组别会作为该文件夹中新导入发票的默认值。")
        helper.setObjectName("sheetHelper")
        helper.setWordWrap(True)
        layout.addWidget(heading)
        layout.addWidget(helper)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        self.date_edit = QDateEdit()
        self.date_edit.setObjectName("sheetDate")
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setMinimumDate(QDate(2026, 1, 1))
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(_to_qdate(purchase_date))
        self.group_combo = QComboBox()
        self.group_combo.setObjectName("sheetGroup")
        self.group_combo.addItems(GROUP_OPTIONS)
        self.group_combo.setCurrentIndex(max(0, self.group_combo.findText(group_name)))
        form.addRow("采购日期", self.date_edit)
        form.addRow("组别", self.group_combo)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Save)
        buttons.setObjectName("sheetButtons")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.accepted.connect(self._accept_if_valid)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _accept_if_valid(self) -> None:
        if self.group_combo.currentIndex() == 0:
            self.group_combo.setProperty("invalid", True)
            self.group_combo.style().unpolish(self.group_combo)
            self.group_combo.style().polish(self.group_combo)
            self.group_combo.setFocus()
            return
        self.accept()

    def values(self) -> tuple[date, str]:
        value = self.date_edit.date()
        return date(value.year(), value.month(), value.day()), self.group_combo.currentText()


class RenamePreviewDialog(QDialog):
    """Confirm a fully evaluated local-file rename plan before it is executed."""

    STATUS_LABELS = {
        "ready": "将重命名",
        "unchanged": "无需修改",
        "skipped": "跳过",
        "conflict": "冲突",
    }

    def __init__(self, items: tuple[InvoiceRenameItem, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("renamePreviewDialog")
        self.setWindowTitle("预览发票文件名")
        self.resize(860, 460)
        self.setMinimumWidth(680)
        ready_count = sum(1 for item in items if item.status == "ready")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        helper = QLabel(
            f"将直接修改原发票文件名。以下 {ready_count} 张会改为“组别首字母-发票号码”，"
            "跳过和冲突项不会被改动。"
        )
        helper.setObjectName("sheetHelper")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        table = QTableWidget(len(items), 4)
        table.setObjectName("renamePreviewTable")
        table.setHorizontalHeaderLabels(["状态", "原文件名", "新文件名", "说明"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setAlternatingRowColors(True)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for row, item in enumerate(items):
            values = [
                self.STATUS_LABELS[item.status],
                item.source.name,
                item.target.name if item.target else "—",
                item.reason or "准备就绪。",
            ]
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                if item.status == "conflict":
                    cell.setForeground(QColor("#D70015"))
                elif item.status == "skipped":
                    cell.setForeground(QColor("#6E6E73"))
                elif item.status == "ready":
                    cell.setForeground(QColor("#248A3D"))
                table.setItem(row, column, cell)
        layout.addWidget(table)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel | QDialogButtonBox.StandardButton.Ok)
        buttons.setObjectName("renamePreviewButtons")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        confirm = buttons.button(QDialogButtonBox.StandardButton.Ok)
        confirm.setText(f"确认重命名 {ready_count} 张")
        confirm.setEnabled(ready_count > 0)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class ImportSourcePicker(QDialog):
    """One file-system picker for either a single application bundle or folders."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("importSourcePicker")
        self.setWindowTitle("导入申请表和发票")
        self.resize(720, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)
        helper = QLabel(
            "可选择 1 份 DOCX 申请表和多张发票，或按住 Ctrl / Shift 选择多个资料文件夹。"
            "文件夹会自动识别其中的申请表和发票。"
        )
        helper.setObjectName("sheetHelper")
        helper.setWordWrap(True)
        layout.addWidget(helper)

        navigation = QHBoxLayout()
        navigation.setSpacing(8)
        desktop_button = QPushButton("桌面")
        desktop_button.setObjectName("importDesktopButton")
        desktop_button.clicked.connect(self.show_desktop)
        computer_button = QPushButton("此电脑")
        computer_button.setObjectName("importComputerButton")
        computer_button.clicked.connect(self.show_computer)
        navigation.addWidget(desktop_button)
        navigation.addWidget(computer_button)
        navigation.addStretch()
        layout.addLayout(navigation)

        self.model = QFileSystemModel(self)
        self.model.setFilter(QDir.Filter.AllEntries | QDir.Filter.NoDotAndDotDot | QDir.Filter.Drives)
        self.model.setRootPath("")
        self.tree = QTreeView()
        self.tree.setObjectName("folderPickerTree")
        self.tree.setModel(self.model)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setAnimated(False)
        self.tree.selectionModel().selectionChanged.connect(self._update_accept_text)
        layout.addWidget(self.tree)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.accept_button = self.buttons.addButton("导入", QDialogButtonBox.ButtonRole.AcceptRole)
        self.accept_button.clicked.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.show_desktop()
        self._update_accept_text()

    def paths(self) -> list[Path]:
        selected = self.tree.selectionModel().selectedRows(0)
        return [Path(self.model.filePath(index)) for index in selected]

    def _update_accept_text(self, *_args) -> None:
        count = len(self.paths())
        self.accept_button.setText(f"导入 {count} 项" if count else "导入")
        self.accept_button.setEnabled(count > 0)

    def show_desktop(self) -> None:
        desktop = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
        if desktop:
            self.tree.setRootIndex(self.model.index(desktop))

    def show_computer(self) -> None:
        self.tree.setRootIndex(self.model.index(""))


class ValidationCard(QFrame):
    """Inline validation summary whose items can focus the related editor field."""

    issue_activated = Signal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("validationCard")
        self.setProperty("state", "neutral")
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.setMaximumHeight(132)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)
        self.summary = QLabel("选择一张发票后显示校验结果")
        self.summary.setObjectName("validationSummary")
        layout.addWidget(self.summary)
        self.list = QListWidget()
        self.list.setObjectName("validationList")
        self.list.setMaximumHeight(92)
        self.list.setVisible(False)
        self.list.itemClicked.connect(self._activate_item)
        layout.addWidget(self.list)

    def set_state(self, state: str, summary: str, issues: list[tuple[str, str, int | None]] | None = None) -> None:
        self.setProperty("state", state)
        self.style().unpolish(self)
        self.style().polish(self)
        self.summary.setText(summary)
        self.list.clear()
        for message, field, item_index in issues or []:
            item = QListWidgetItem(message)
            item.setData(Qt.ItemDataRole.UserRole, (field, -1 if item_index is None else item_index))
            self.list.addItem(item)
        self.list.setVisible(self.list.count() > 0)

    def setPlainText(self, text: str) -> None:
        self.set_state("neutral", text)

    def clear(self) -> None:
        self.set_state("neutral", "")

    def toPlainText(self) -> str:
        messages = [self.list.item(index).text() for index in range(self.list.count())]
        return "\n".join(messages) if messages else self.summary.text()

    def _activate_item(self, item: QListWidgetItem) -> None:
        field, item_index = item.data(Qt.ItemDataRole.UserRole)
        self.issue_activated.emit(field, item_index)


def _status_icon(color: str) -> QIcon:
    pixmap = QPixmap(14, 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor(color), 1))
    painter.setBrush(QColor(color))
    painter.drawEllipse(QRectF(3, 3, 8, 8))
    painter.end()
    return QIcon(pixmap)


class RecognitionWorker(QThread):
    progress = Signal(str, str)
    parsed = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, service: InvoiceService, jobs: list[tuple[str, Path]]) -> None:
        super().__init__()
        self.service = service
        self.jobs = jobs

    def run(self) -> None:
        for entry_id, path in self.jobs:
            self.progress.emit(entry_id, f"识别中：{path.name}")
            try:
                self.parsed.emit(entry_id, self.service.parse_file(path))
            except Exception as error:
                self.failed.emit(entry_id, str(error))


class ZoomablePreviewScrollArea(QScrollArea):
    """A preview scroll area whose wheel zoom remains anchored at the cursor."""

    zoom_requested = Signal(float, QPoint)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta:
            self.zoom_requested.emit(1.25 if delta > 0 else 0.8, event.position().toPoint())
            event.accept()
            return
        super().wheelEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, initial_files: list[str] | None = None) -> None:
        super().__init__()
        self.service = InvoiceService()
        self.folders: list[InvoiceFolder] = []
        self.workers: list[RecognitionWorker] = []
        self._recognition_queue: list[tuple[str, Path]] = []
        self._pending_folder_matches: dict[str, PendingFolderMatch] = {}
        self._loading = False
        self._displayed_entry_id: str | None = None
        self._displayed_folder_id: str | None = None
        self._folder_items: dict[str, QTreeWidgetItem] = {}
        self._entry_items: dict[str, QTreeWidgetItem] = {}
        self._preview_source: QPixmap | None = None
        self._preview_zoom = 1.0
        self._last_rename_items: tuple[InvoiceRenameItem, ...] = ()
        self.setWindowTitle("本地发票 · 完全离线")
        self.resize(1500, 900)
        self.setMinimumSize(1180, 720)
        self.setAcceptDrops(True)
        self._build_ui()
        self._apply_theme()
        self._enable_windows_backdrop()
        self._update_batch_summary()
        if initial_files:
            self.add_files([Path(item) for item in initial_files])

    def _build_ui(self) -> None:
        toolbar = QToolBar("操作")
        toolbar.setObjectName("mainToolbar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setIconSize(QSize(17, 17))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        app_title = QLabel("本地发票")
        app_title.setObjectName("appTitle")
        offline_badge = QLabel("●  完全离线")
        offline_badge.setObjectName("offlineBadge")
        toolbar.addWidget(app_title)
        toolbar.addWidget(offline_badge)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)
        self.toolbar_batch_summary = QLabel()
        self.toolbar_batch_summary.setObjectName("toolbarBatchSummary")
        toolbar.addWidget(self.toolbar_batch_summary)

        self.create_folder_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder), "新建日期分组", self
        )
        self.create_folder_action.setShortcut(QKeySequence("Ctrl+N"))
        self.create_folder_action.triggered.connect(self.create_folder)
        toolbar.addAction(self.create_folder_action)
        self.import_bundle_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton), "导入申请表和发票", self
        )
        self.import_bundle_action.setShortcut(QKeySequence("Ctrl+I"))
        self.import_bundle_action.triggered.connect(self.choose_application_bundle)
        toolbar.addAction(self.import_bundle_action)
        self.add_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton), "导入发票", self
        )
        self.add_action.setShortcut(QKeySequence("Ctrl+O"))
        self.add_action.triggered.connect(self.choose_files)
        toolbar.addAction(self.add_action)
        self.rename_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView), "批量重命名", self
        )
        self.rename_action.setToolTip("预览并按“组别首字母-发票号码”重命名已确认发票")
        self.rename_action.triggered.connect(self.preview_invoice_renames)
        toolbar.addAction(self.rename_action)
        self.undo_rename_action = QAction("撤销上次重命名", self)
        self.undo_rename_action.setToolTip("仅撤销本次运行中最近一次成功的批量重命名")
        self.undo_rename_action.triggered.connect(self.undo_last_invoice_rename)
        toolbar.addAction(self.undo_rename_action)
        self.export_action = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton), "导出 Excel", self
        )
        self.export_action.setShortcut(QKeySequence("Ctrl+S"))
        self.export_action.triggered.connect(self.export_current_batch)
        toolbar.addAction(self.export_action)
        self.export_tool_button = toolbar.widgetForAction(self.export_action)
        if self.export_tool_button:
            self.export_tool_button.setObjectName("exportToolButton")

        self.confirm_action = QAction("确认当前发票", self)
        self.confirm_action.setShortcut(QKeySequence("Ctrl+Return"))
        self.confirm_action.triggered.connect(self.confirm_current)
        self.addAction(self.confirm_action)
        self.cancel_confirm_action = QAction("取消确认", self)
        self.cancel_confirm_action.setShortcut(QKeySequence("Ctrl+Shift+Return"))
        self.cancel_confirm_action.triggered.connect(self.cancel_confirmation)
        self.addAction(self.cancel_confirm_action)
        self.edit_folder_action = QAction("修改选中文件夹", self)
        self.edit_folder_action.setShortcut(QKeySequence("Ctrl+E"))
        self.edit_folder_action.triggered.connect(self.edit_selected_folder)
        self.addAction(self.edit_folder_action)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setObjectName("mainSplitter")
        splitter.setHandleWidth(1)
        splitter.addWidget(self._build_file_panel())
        splitter.addWidget(self._build_preview_panel())
        splitter.addWidget(self._build_editor_panel())
        splitter.setChildrenCollapsible(False)
        splitter.setSizes([300, 520, 680])
        self.setCentralWidget(splitter)
        self.statusBar().setObjectName("appStatusBar")
        self.statusBar().showMessage("创建第一个日期分组后即可导入 PDF、JPG 或 PNG 发票。")

    def _build_file_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("sidePanel")
        panel.setMinimumWidth(270)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(9)
        header = QHBoxLayout()
        title = QLabel("当前批次")
        title.setObjectName("panelTitle")
        header.addWidget(title)
        header.addStretch()
        self.sidebar_more_button = QToolButton()
        self.sidebar_more_button.setObjectName("sidebarMoreButton")
        self.sidebar_more_button.setText("•••")
        self.sidebar_more_button.setToolTip("选中项的更多操作")
        self.sidebar_more_button.clicked.connect(self._open_selected_menu)
        header.addWidget(self.sidebar_more_button)
        layout.addLayout(header)
        self.batch_summary = QLabel()
        self.batch_summary.setObjectName("batchSummary")
        self.batch_summary.setWordWrap(True)
        layout.addWidget(self.batch_summary)
        self.batch_progress = QProgressBar()
        self.batch_progress.setObjectName("batchProgress")
        self.batch_progress.setRange(0, 1)
        self.batch_progress.setValue(0)
        self.batch_progress.setTextVisible(False)
        layout.addWidget(self.batch_progress)
        self.empty_batch_hint = QLabel("创建第一个日期分组后\n即可导入并复核发票")
        self.empty_batch_hint.setObjectName("emptyBatchHint")
        self.empty_batch_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_batch_hint.setWordWrap(True)
        layout.addWidget(self.empty_batch_hint)
        self.batch_tree = QTreeWidget()
        self.batch_tree.setObjectName("batchTree")
        self.batch_tree.setHeaderHidden(True)
        self.batch_tree.setIndentation(16)
        self.batch_tree.setIconSize(QSize(14, 14))
        self.batch_tree.setAnimated(False)
        self.batch_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.batch_tree.customContextMenuRequested.connect(self._show_tree_menu)
        self.batch_tree.currentItemChanged.connect(self.show_tree_item)
        self.batch_root = QTreeWidgetItem(["总批次"])
        self.batch_root.setData(0, Qt.ItemDataRole.UserRole, ("root", ""))
        self.batch_root.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_DirHomeIcon))
        self.batch_root.setExpanded(True)
        self.batch_tree.addTopLevelItem(self.batch_root)
        layout.addWidget(self.batch_tree)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("previewPanel")
        panel.setMinimumWidth(390)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(9)
        preview_header = QHBoxLayout()
        title = QLabel("原始票据")
        title.setObjectName("panelTitle")
        preview_header.addWidget(title)
        preview_header.addStretch()
        zoom_out = QPushButton("−")
        zoom_out.setObjectName("zoomButton")
        zoom_out.setToolTip("缩小")
        zoom_out.clicked.connect(self.zoom_out)
        zoom_fit = QPushButton("适合")
        zoom_fit.setObjectName("zoomFitButton")
        zoom_fit.setToolTip("适合窗口")
        zoom_fit.clicked.connect(self.zoom_fit)
        zoom_in = QPushButton("＋")
        zoom_in.setObjectName("zoomButton")
        zoom_in.setToolTip("放大")
        zoom_in.clicked.connect(self.zoom_in)
        self.zoom_label = QLabel("100%")
        self.zoom_label.setObjectName("zoomLabel")
        preview_header.addWidget(zoom_out)
        preview_header.addWidget(zoom_fit)
        preview_header.addWidget(zoom_in)
        preview_header.addWidget(self.zoom_label)
        layout.addLayout(preview_header)
        self.preview_canvas = QWidget()
        self.preview_canvas.setObjectName("previewCanvas")
        self.preview = QLabel("尚未选择发票", self.preview_canvas)
        self.preview.setObjectName("previewPaper")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumSize(1, 1)
        self.preview_scroll = ZoomablePreviewScrollArea()
        self.preview_scroll.setObjectName("previewScroll")
        self.preview_scroll.setWidgetResizable(False)
        self.preview_scroll.setWidget(self.preview_canvas)
        self.preview_scroll.zoom_requested.connect(self.zoom_at)
        layout.addWidget(self.preview_scroll)
        self.drop_overlay = QLabel(self.preview_scroll.viewport())
        self.drop_overlay.setObjectName("dropOverlay")
        self.drop_overlay.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_overlay.setWordWrap(True)
        self.drop_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.drop_overlay.hide()
        return panel

    def _build_editor_panel(self) -> QWidget:
        panel = QWidget()
        panel.setObjectName("editorPanel")
        panel.setMinimumWidth(520)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(9)

        inspector_header = QHBoxLayout()
        inspector_titles = QVBoxLayout()
        inspector_titles.setSpacing(1)
        self.current_file_label = QLabel("发票检查器")
        self.current_file_label.setObjectName("panelTitle")
        self.current_context_label = QLabel("从左侧选择一张发票")
        self.current_context_label.setObjectName("panelSubtitle")
        inspector_titles.addWidget(self.current_file_label)
        inspector_titles.addWidget(self.current_context_label)
        inspector_header.addLayout(inspector_titles)
        inspector_header.addStretch()
        self.invoice_status_badge = QLabel("未选择")
        self.invoice_status_badge.setObjectName("statusBadge")
        self.invoice_status_badge.setProperty("state", "neutral")
        inspector_header.addWidget(self.invoice_status_badge, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(inspector_header)

        metadata = QFrame()
        metadata.setObjectName("metadataCard")
        form = QGridLayout(metadata)
        form.setContentsMargins(12, 10, 12, 12)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(5)
        self.invoice_number = QLineEdit()
        self.total_with_tax = QLineEdit()
        self.purchase_date = QDateEdit()
        self.purchase_date.setCalendarPopup(True)
        self.purchase_date.setMinimumDate(QDate(2026, 1, 1))
        self.purchase_date.setDisplayFormat("yyyy-MM-dd")
        self.group_name = QComboBox()
        self.group_name.addItems(GROUP_OPTIONS)
        self.invoice_number.textChanged.connect(self.mark_unconfirmed)
        self.total_with_tax.textChanged.connect(self.mark_unconfirmed)
        self.purchase_date.dateChanged.connect(self.mark_unconfirmed)
        self.group_name.currentTextChanged.connect(self.mark_unconfirmed)
        self.confirmed = QCheckBox("已人工确认")
        self.confirmed.setEnabled(False)
        self.confirmed.setObjectName("confirmedBadge")
        self.confirmed.hide()
        labels = []
        for text in ("发票号码", "价税合计（元）", "采购日期", "分组"):
            label = QLabel(text)
            label.setObjectName("fieldLabel")
            labels.append(label)
        form.addWidget(labels[0], 0, 0)
        form.addWidget(labels[1], 0, 1)
        form.addWidget(self.invoice_number, 1, 0)
        form.addWidget(self.total_with_tax, 1, 1)
        form.addWidget(labels[2], 2, 0)
        form.addWidget(labels[3], 2, 1)
        form.addWidget(self.purchase_date, 3, 0)
        form.addWidget(self.group_name, 3, 1)
        form.setColumnStretch(0, 3)
        form.setColumnStretch(1, 2)
        layout.addWidget(metadata)
        self.invoice_metadata = metadata

        self.application_card = QFrame()
        self.application_card.setObjectName("metadataCard")
        application_form = QGridLayout(self.application_card)
        application_form.setContentsMargins(12, 10, 12, 12)
        application_form.setHorizontalSpacing(12)
        application_form.setVerticalSpacing(5)
        self.application_number = QLineEdit()
        self.application_applicant = QLineEdit()
        self.application_date = QDateEdit()
        self.application_date.setCalendarPopup(True)
        self.application_date.setMinimumDate(QDate(2026, 1, 1))
        self.application_date.setDisplayFormat("yyyy-MM-dd")
        self.application_group = QComboBox()
        self.application_group.addItems(GROUP_OPTIONS)
        self.application_amount = QLineEdit()
        self.application_payment_date_enabled = QCheckBox("已填写付款日期")
        self.application_payment_date = QDateEdit()
        self.application_payment_date.setCalendarPopup(True)
        self.application_payment_date.setMinimumDate(QDate(2026, 1, 1))
        self.application_payment_date.setDisplayFormat("yyyy-MM-dd")
        self.application_invoice_total = QLineEdit()
        self.application_invoice_total.setReadOnly(True)
        self.application_difference = QLineEdit()
        self.application_difference.setReadOnly(True)
        self.application_difference_acknowledged = QCheckBox("已人工确认金额差异")
        self.application_open_source = QPushButton("打开原申请表")
        self.application_open_source.clicked.connect(self.open_application_source)
        self.application_confirm_button = QPushButton("确认申请单信息")
        self.application_confirm_button.setProperty("role", "primary")
        self.application_confirm_button.clicked.connect(self.confirm_application_current)
        for widget in (self.application_number, self.application_applicant, self.application_amount):
            widget.textChanged.connect(self.mark_application_unconfirmed)
        self.application_date.dateChanged.connect(self.mark_application_unconfirmed)
        self.application_group.currentTextChanged.connect(self.mark_application_unconfirmed)
        self.application_payment_date_enabled.toggled.connect(self.mark_application_unconfirmed)
        self.application_payment_date.dateChanged.connect(self.mark_application_unconfirmed)
        labels = [QLabel(text) for text in ("申请单编号", "申请人", "采购时间", "部门", "采购单金额（元）", "付款日期", "发票总价（元）", "差额（发票-采购）")]
        for label in labels:
            label.setObjectName("fieldLabel")
        application_form.addWidget(labels[0], 0, 0)
        application_form.addWidget(labels[1], 0, 1)
        application_form.addWidget(self.application_number, 1, 0)
        application_form.addWidget(self.application_applicant, 1, 1)
        application_form.addWidget(labels[2], 2, 0)
        application_form.addWidget(labels[3], 2, 1)
        application_form.addWidget(self.application_date, 3, 0)
        application_form.addWidget(self.application_group, 3, 1)
        application_form.addWidget(labels[4], 4, 0)
        application_form.addWidget(labels[5], 4, 1)
        application_form.addWidget(self.application_amount, 5, 0)
        payment_layout = QHBoxLayout()
        payment_layout.setContentsMargins(0, 0, 0, 0)
        payment_layout.addWidget(self.application_payment_date_enabled)
        payment_layout.addWidget(self.application_payment_date)
        application_form.addLayout(payment_layout, 5, 1)
        application_form.addWidget(labels[6], 6, 0)
        application_form.addWidget(labels[7], 6, 1)
        application_form.addWidget(self.application_invoice_total, 7, 0)
        application_form.addWidget(self.application_difference, 7, 1)
        application_form.addWidget(self.application_difference_acknowledged, 8, 0)
        source_actions = QHBoxLayout()
        source_actions.addWidget(self.application_open_source)
        source_actions.addStretch()
        source_actions.addWidget(self.application_confirm_button)
        application_form.addLayout(source_actions, 8, 1)
        self.application_card.hide()
        layout.addWidget(self.application_card)

        self.table = QTableWidget(0, len(ITEM_HEADERS))
        self.table.setObjectName("invoiceTable")
        self.table.setHorizontalHeaderLabels(ITEM_HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self._table_edit_triggers = self.table.editTriggers()
        self.table.itemChanged.connect(self.mark_unconfirmed)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(31)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 105)
        header.resizeSection(2, 54)
        for column in range(3, 8):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
            header.resizeSection(column, 88 if column != 6 else 104)
        layout.addWidget(self.table)
        self.table_actions_container = QWidget()
        table_actions = QHBoxLayout(self.table_actions_container)
        self.add_item_button = QPushButton("＋")
        self.add_item_button.setObjectName("tableMiniButton")
        self.add_item_button.setToolTip("添加明细")
        self.add_item_button.clicked.connect(self.add_item_row)
        self.remove_item_button = QPushButton("−")
        self.remove_item_button.setObjectName("tableMiniButton")
        self.remove_item_button.setToolTip("删除选中明细")
        self.remove_item_button.clicked.connect(self.remove_item_row)
        table_actions.addWidget(self.add_item_button)
        table_actions.addWidget(self.remove_item_button)
        table_actions.addStretch()
        layout.addWidget(self.table_actions_container)

        self.issues = ValidationCard()
        self.issues.issue_activated.connect(self._focus_issue)
        layout.addWidget(self.issues)

        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(10, 8, 8, 8)
        action_layout.setSpacing(8)
        self.action_state_label = QLabel("请选择一张发票")
        self.action_state_label.setObjectName("actionStateLabel")
        action_layout.addWidget(self.action_state_label)
        action_layout.addStretch()
        self.cancel_confirm_button = QPushButton("取消确认")
        self.cancel_confirm_button.setProperty("role", "secondary")
        self.cancel_confirm_button.setToolTip("Ctrl+Shift+Enter")
        self.cancel_confirm_button.clicked.connect(self.cancel_confirmation)
        self.confirm_button = QPushButton("确认并进入下一张")
        self.confirm_button.setProperty("role", "primary")
        self.confirm_button.setToolTip("Ctrl+Enter")
        self.confirm_button.clicked.connect(self.confirm_current)
        action_layout.addWidget(self.cancel_confirm_button)
        action_layout.addWidget(self.confirm_button)
        layout.addWidget(action_bar)
        self.invoice_action_bar = action_bar
        return panel

    def _apply_theme(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QDialog { background: #F2F2F7; color: #1C1C1E; font-family: 'Segoe UI Variable', 'Segoe UI', 'Microsoft YaHei UI'; font-size: 13px; }
            QToolBar#mainToolbar { background: rgba(248, 248, 250, 245); border: none; border-bottom: 1px solid #D9D9DE; padding: 8px 13px; spacing: 7px; }
            QToolBar#mainToolbar QToolButton { min-height: 29px; color: #3A3A3C; border: 1px solid transparent; border-radius: 8px; padding: 1px 10px; font-weight: 600; }
            QToolBar#mainToolbar QToolButton:hover { background: #E9E9ED; }
            QToolBar#mainToolbar QToolButton:pressed { background: #DCDCE2; padding-top: 2px; }
            QToolBar#mainToolbar QToolButton:disabled { color: #AEAEB2; }
            QToolBar#mainToolbar QToolButton#exportToolButton[ready="true"] { color: #FFFFFF; background: #007AFF; }
            QLabel#appTitle { color: #1C1C1E; font-size: 16px; font-weight: 700; padding-right: 8px; }
            QLabel#offlineBadge { color: #248A3D; background: #EAF7ED; border-radius: 9px; padding: 4px 8px; font-size: 11px; font-weight: 600; }
            QLabel#toolbarBatchSummary { color: #6E6E73; padding: 0 12px; }
            QSplitter#mainSplitter::handle { background: #D9D9DE; }
            QWidget#sidePanel { background: #F6F6F8; }
            QWidget#previewPanel { background: #F2F2F7; }
            QWidget#editorPanel { background: #FFFFFF; }
            QLabel#panelTitle { color: #1C1C1E; font-size: 17px; font-weight: 700; }
            QLabel#panelSubtitle { color: #8E8E93; font-size: 12px; }
            QLabel#fieldLabel { color: #6E6E73; font-size: 11px; font-weight: 600; }
            QLabel#batchSummary { color: #3A3A3C; padding: 2px 0; }
            QLabel#emptyBatchHint { color: #8E8E93; background: #FFFFFF; border: 1px dashed #D1D1D6; border-radius: 12px; padding: 16px; }
            QProgressBar#batchProgress { min-height: 4px; max-height: 4px; border: none; border-radius: 2px; background: #E5E5EA; }
            QProgressBar#batchProgress::chunk { border-radius: 2px; background: #007AFF; }
            QToolButton#sidebarMoreButton { min-width: 30px; min-height: 28px; border: none; border-radius: 7px; color: #6E6E73; font-weight: 700; }
            QToolButton#sidebarMoreButton:hover { background: #E5E5EA; color: #1C1C1E; }
            QTreeWidget#batchTree { border: none; background: transparent; padding: 2px 0; outline: none; }
            QTreeWidget#batchTree::item { min-height: 38px; border-radius: 8px; padding: 5px 7px; color: #3A3A3C; }
            QTreeWidget#batchTree::item:hover { background: #ECECF0; }
            QTreeWidget#batchTree::item:selected { background: #DCEBFF; color: #0A60C7; }
            QTreeWidget#batchTree::branch { background: transparent; }
            QTreeWidget#batchTree::branch:selected { background: #DCEBFF; }
            QFrame#metadataCard { background: #F7F7F9; border: 1px solid #E5E5EA; border-radius: 12px; }
            QLineEdit, QComboBox, QDateEdit { min-height: 32px; border: 1px solid #D1D1D6; border-radius: 8px; padding: 0 9px; background: #FFFFFF; color: #1C1C1E; selection-background-color: #007AFF; }
            QLineEdit:focus, QComboBox:focus, QDateEdit:focus { border: 2px solid #64A9FF; }
            QLineEdit:read-only { background: #F2F2F7; color: #6E6E73; }
            QComboBox:disabled, QDateEdit:disabled { background: #F2F2F7; color: #8E8E93; border-color: #E5E5EA; }
            QComboBox[invalid="true"] { border: 2px solid #FF3B30; background: #FFF4F3; }
            QLabel#statusBadge { border-radius: 9px; padding: 4px 9px; font-size: 11px; font-weight: 700; }
            QLabel#statusBadge[state="neutral"] { color: #6E6E73; background: #EFEFF4; }
            QLabel#statusBadge[state="working"] { color: #0064D9; background: #E7F1FF; }
            QLabel#statusBadge[state="pending"] { color: #A85A00; background: #FFF2D8; }
            QLabel#statusBadge[state="success"] { color: #248A3D; background: #EAF7ED; }
            QLabel#statusBadge[state="error"] { color: #D70015; background: #FFE9E7; }
            QWidget#previewCanvas { background: #EDEDF1; }
            QLabel#previewPaper { background: #FFFFFF; color: #8E8E93; border: 1px solid #D9D9DE; }
            QScrollArea#previewScroll { border: none; border-radius: 12px; background: #EDEDF1; }
            QPushButton#zoomButton, QPushButton#tableMiniButton { min-width: 30px; max-width: 30px; min-height: 28px; max-height: 28px; padding: 0; font-size: 16px; }
            QPushButton#zoomFitButton { min-height: 28px; max-height: 28px; padding: 0 10px; }
            QLabel#zoomLabel { min-width: 40px; color: #8E8E93; font-size: 11px; qproperty-alignment: AlignCenter; }
            QLabel#dropOverlay { color: #FFFFFF; background: rgba(0, 122, 255, 220); border: 2px solid rgba(255, 255, 255, 190); border-radius: 16px; font-size: 17px; font-weight: 700; padding: 24px; }
            QTableWidget#invoiceTable { border: 1px solid #E5E5EA; border-radius: 10px; gridline-color: #EFEFF4; alternate-background-color: #FAFAFC; selection-background-color: #DCEBFF; selection-color: #1C1C1E; }
            QTableWidget#invoiceTable::item { padding: 4px 6px; }
            QHeaderView::section { background: #F7F7F9; color: #6E6E73; border: none; border-bottom: 1px solid #E5E5EA; padding: 8px 5px; font-size: 11px; font-weight: 700; }
            QFrame#validationCard { border: 1px solid #E5E5EA; border-radius: 12px; background: #F7F7F9; }
            QFrame#validationCard[state="success"] { border-color: #B8E8C2; background: #F0FAF2; }
            QFrame#validationCard[state="error"] { border-color: #FFD0CC; background: #FFF4F3; }
            QFrame#validationCard[state="warning"] { border-color: #FFE0A8; background: #FFF9ED; }
            QLabel#validationSummary { color: #3A3A3C; font-weight: 700; }
            QListWidget#validationList { border: none; background: transparent; color: #6E6E73; outline: none; }
            QListWidget#validationList::item { min-height: 24px; border-radius: 5px; padding: 2px 5px; }
            QListWidget#validationList::item:hover { background: rgba(0, 122, 255, 24); color: #0A60C7; }
            QFrame#actionBar { background: #F7F7F9; border: 1px solid #E5E5EA; border-radius: 12px; }
            QLabel#actionStateLabel { color: #6E6E73; font-weight: 600; }
            QPushButton { min-height: 32px; border: 1px solid #D1D1D6; border-radius: 8px; padding: 0 12px; background: #FFFFFF; color: #3A3A3C; font-weight: 600; }
            QPushButton:hover { background: #F2F2F7; }
            QPushButton:pressed { background: #E5E5EA; padding-top: 1px; }
            QPushButton:disabled { background: #F7F7F9; color: #AEAEB2; border-color: #E5E5EA; }
            QPushButton[role="primary"] { background: #007AFF; color: #FFFFFF; border-color: #007AFF; }
            QPushButton[role="primary"]:hover { background: #006EE6; border-color: #006EE6; }
            QPushButton[role="primary"]:pressed { background: #005FC7; border-color: #005FC7; }
            QPushButton[role="secondary"] { color: #3A3A3C; background: #FFFFFF; }
            QDialog#folderSheet { background: #F7F7F9; }
            QLabel#sheetTitle { color: #1C1C1E; font-size: 20px; font-weight: 700; }
            QLabel#sheetHelper { color: #6E6E73; }
            QStatusBar#appStatusBar { background: #F8F8FA; color: #6E6E73; border-top: 1px solid #D9D9DE; padding: 3px 10px; }
            QMenu { background: #FFFFFF; color: #1C1C1E; border: 1px solid #D9D9DE; border-radius: 9px; padding: 5px; }
            QMenu::item { min-height: 26px; border-radius: 6px; padding: 3px 22px; }
            QMenu::item:selected { background: #007AFF; color: #FFFFFF; }
            """
        )

    def _enable_windows_backdrop(self) -> None:
        """Request the Windows 11 Mica title-bar material, with a silent Windows 10 fallback."""
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            backdrop = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, 38, ctypes.byref(backdrop), ctypes.sizeof(backdrop)
            )
        except (AttributeError, OSError, TypeError, ValueError):
            return

    def choose_files(self) -> None:
        if self._selected_folder() is None:
            self.statusBar().showMessage("请先新建或选择一个日期分组，再导入发票。", 5000)
            return
        names, _ = QFileDialog.getOpenFileNames(self, "选择发票", "", "发票文件 (*.pdf *.jpg *.jpeg *.png)")
        self.add_files([Path(name) for name in names])

    def choose_application_bundle(self) -> None:
        picker = ImportSourcePicker(self)
        if picker.exec() != QDialog.DialogCode.Accepted:
            return
        paths = picker.paths()
        if not paths:
            return
        folders = [path for path in paths if path.is_dir()]
        if folders and len(folders) != len(paths):
            QMessageBox.warning(self, "请分开导入", "请一次选择文件，或一次选择文件夹；请分两次操作。")
            return
        if folders:
            self.import_folders(folders)
            return
        self.import_application_bundle(paths)

    def import_application_bundle(self, paths: list[Path]) -> None:
        applications = [path for path in paths if path.suffix.lower() in SUPPORTED_APPLICATION_EXTENSIONS]
        invoices = [path for path in paths if path.suffix.lower() in SUPPORTED_EXTENSIONS]
        if len(applications) != 1 or not invoices:
            QMessageBox.warning(self, "资料不完整", "请一次选择恰好 1 份 DOCX 申请表和至少 1 张发票。")
            return
        try:
            existing = self._application_numbers()
            application = self.service.parse_application(applications[0], existing)
        except Exception as error:
            QMessageBox.critical(self, "申请表识别失败", str(error))
            return
        purchase_date = application.purchase_date or max(date.today(), date(2026, 1, 1))
        folder = self._create_folder_with_values(purchase_date, application.group_name, application=application)
        self._add_invoice_paths(folder, invoices)
        self.statusBar().showMessage(f"已导入申请表和 {len(invoices)} 张发票，请先复核申请单信息。")

    def import_folders(self, paths: list[Path], *, show_summary: bool = True) -> tuple[int, int, list[tuple[Path, str]]]:
        """Create one application folder for every valid selected source directory."""
        self.save_current()
        skipped: list[tuple[Path, str]] = []
        candidates: list[Path] = []
        selected_keys: set[str] = set()
        imported_keys = self._imported_folder_keys()
        for path in paths:
            key = self._path_key(path)
            if key in selected_keys:
                skipped.append((path, "重复选择。"))
            elif key in imported_keys:
                skipped.append((path, "已在当前批次导入。"))
            else:
                selected_keys.add(key)
                candidates.append(path)

        jobs: list[tuple[str, Path]] = []
        imported_count = 0
        for source_folder in sorted(candidates, key=lambda path: self._path_key(path)):
            scanned = scan_import_folder(source_folder)
            if not scanned.valid:
                skipped.append((source_folder, scanned.error))
                continue
            application_folders: list[InvoiceFolder] = []
            for source_application in scanned.application_files:
                try:
                    application = self.service.parse_application(source_application, self._application_numbers())
                except Exception as error:
                    skipped.append((source_application, f"申请表无法读取：{error}"))
                    continue
                purchase_date = application.purchase_date or max(date.today(), date(2026, 1, 1))
                folder = self._create_folder_with_values(purchase_date, application.group_name, application=application)
                folder.source_folder = source_folder
                application_folders.append(folder)
            if not application_folders:
                continue
            if len(scanned.application_files) == 1 and len(application_folders) == 1:
                jobs.extend(self._prepare_invoice_paths(application_folders[0], scanned.invoice_files))
                imported_count += 1
                continue
            unassigned = self._create_unassigned_folder(source_folder)
            pending_jobs = self._prepare_invoice_paths(unassigned, scanned.invoice_files)
            jobs.extend(pending_jobs)
            self._pending_folder_matches[self._path_key(source_folder)] = PendingFolderMatch(
                source_folder=source_folder,
                application_folder_ids=[folder.folder_id for folder in application_folders],
                unassigned_folder_id=unassigned.folder_id,
                entry_ids=[entry_id for entry_id, _path in pending_jobs],
            )
            imported_count += 1

        if jobs:
            self._enqueue_recognition(jobs)
        invoice_count = len(jobs)
        summary = self._folder_import_summary(imported_count, invoice_count, skipped)
        self.statusBar().showMessage(summary.replace("\n", " "), 10000)
        if show_summary:
            QMessageBox.information(self, "文件夹导入结果", summary)
        return imported_count, invoice_count, skipped

    def add_files(self, paths: list[Path]) -> None:
        folder = self._selected_folder()
        if folder is None:
            self.statusBar().showMessage("请先新建或选择一个日期和分组文件夹。")
            return
        accepted = [path for path in paths if path.suffix.lower() in SUPPORTED_EXTENSIONS and path.exists()]
        if not accepted:
            return
        self.save_current()
        self._add_invoice_paths(folder, paths)

    def _add_invoice_paths(self, folder: InvoiceFolder, paths: list[Path]) -> None:
        jobs = self._prepare_invoice_paths(folder, paths)
        if jobs:
            self._enqueue_recognition(jobs)
            self.statusBar().showMessage(f"已导入 {len(jobs)} 张发票到“{self._folder_label(folder)}”。")

    def _prepare_invoice_paths(self, folder: InvoiceFolder, paths: list[Path]) -> list[tuple[str, Path]]:
        accepted = [path for path in paths if path.suffix.lower() in SUPPORTED_EXTENSIONS and path.exists()]
        if not accepted:
            return []
        if folder.application is not None:
            folder.application.confirmed = False
            folder.application.difference_acknowledged = False
        jobs: list[tuple[str, Path]] = []
        for path in accepted:
            entry = InvoiceEntry(source_file=path)
            folder.entries.append(entry)
            jobs.append((entry.entry_id, path))
            self._add_entry_item(folder, entry)
        self._update_folder_item(folder)
        return jobs

    def _enqueue_recognition(self, jobs: list[tuple[str, Path]]) -> None:
        self._recognition_queue.extend(jobs)
        self._start_queued_recognition()

    def _start_queued_recognition(self) -> None:
        if self.workers or not self._recognition_queue:
            return
        jobs = self._recognition_queue
        self._recognition_queue = []
        worker = RecognitionWorker(self.service, jobs)
        worker.progress.connect(self._set_entry_text)
        worker.parsed.connect(self._receive_record)
        worker.failed.connect(self._receive_error)
        worker.finished.connect(lambda current=worker: self._recognition_finished(current))
        self.workers.append(worker)
        worker.start()

    def create_folder(self) -> None:
        today = date.today()
        initial_date = max(today, date(2026, 1, 1))
        sheet = FolderSheet(self, title="新建日期分组", purchase_date=initial_date)
        if sheet.exec() != QDialog.DialogCode.Accepted:
            return
        purchase_date, group_name = sheet.values()
        self._create_folder_with_values(purchase_date, group_name)

    def _create_folder_with_values(
        self,
        purchase_date: date,
        group_name: str,
        application: PurchaseApplication | None = None,
    ) -> InvoiceFolder:
        if application is None:
            application = PurchaseApplication(purchase_date=purchase_date, group_name=group_name)
        folder = InvoiceFolder(
            purchase_date=purchase_date,
            group_name=group_name,
            creation_order=len(self.folders) + 1,
            application=application,
        )
        self.folders.append(folder)
        item = QTreeWidgetItem([self._folder_label(folder)])
        item.setData(0, Qt.ItemDataRole.UserRole, ("folder", folder.folder_id))
        item.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
        self.batch_root.addChild(item)
        self._folder_items[folder.folder_id] = item
        self.batch_root.setExpanded(True)
        self.batch_tree.setCurrentItem(item)
        self._update_batch_summary()
        self.statusBar().showMessage(f"已新建文件夹：{self._folder_label(folder)}。现在可导入多张发票。")
        return folder

    def _create_unassigned_folder(self, source_folder: Path) -> InvoiceFolder:
        folder = InvoiceFolder(
            purchase_date=max(date.today(), date(2026, 1, 1)),
            group_name="待分配",
            creation_order=len(self.folders) + 1,
            source_folder=source_folder,
            is_unassigned=True,
        )
        self.folders.append(folder)
        item = QTreeWidgetItem([self._folder_label(folder)])
        item.setData(0, Qt.ItemDataRole.UserRole, ("folder", folder.folder_id))
        item.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
        self.batch_root.addChild(item)
        self._folder_items[folder.folder_id] = item
        self.batch_root.setExpanded(True)
        self.batch_tree.setCurrentItem(item)
        self._update_batch_summary()
        return folder

    def edit_selected_folder(self) -> None:
        item = self.batch_tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not data or data[0] != "folder":
            QMessageBox.warning(self, "请选择文件夹", "请先选择需要修改的文件夹。")
            return
        folder = next((value for value in self.folders if value.folder_id == data[1]), None)
        if folder is None or folder.is_unassigned:
            return
        sheet = FolderSheet(
            self,
            title="修改日期分组",
            purchase_date=folder.purchase_date,
            group_name=folder.group_name,
        )
        if sheet.exec() != QDialog.DialogCode.Accepted:
            return
        purchase_date, group_name = sheet.values()
        self._update_folder_values(folder, purchase_date, group_name)

    def _update_folder_values(self, folder: InvoiceFolder, purchase_date: date, group_name: str) -> None:
        old_date, old_group = folder.purchase_date, folder.group_name
        folder.purchase_date, folder.group_name = purchase_date, group_name
        if folder.application is not None:
            application = folder.application
            existing = self._application_numbers(exclude_folder_id=folder.folder_id)
            application.purchase_date = purchase_date
            application.group_name = group_name
            if application.number_is_automatic:
                application.application_number = generate_application_number(group_name, purchase_date, existing)
            application.confirmed = False
            application.difference_acknowledged = False
        for entry in folder.entries:
            if entry.record is None:
                continue
            # 只同步仍采用文件夹默认值的发票，保留右侧单独修改过的例外。
            if entry.record.purchase_date == old_date and entry.record.group_name == old_group:
                entry.record.purchase_date = folder.purchase_date
                entry.record.group_name = folder.group_name
        self._update_folder_item(folder)
        if self._displayed_entry_id:
            found = self._entry_by_id(self._displayed_entry_id)
            if found and found[0] is folder:
                self.show_entry(self._displayed_entry_id)
        self.statusBar().showMessage(f"已修改文件夹：{self._folder_label(folder)}。")

    def _folder_label(self, folder: InvoiceFolder) -> str:
        total = sum(
            (entry.record.total_with_tax or Decimal("0"))
            for entry in folder.entries
            if entry.record is not None
        )
        waiting = sum(1 for entry in folder.entries if entry.record is None and not entry.error)
        confirmed = sum(1 for entry in folder.entries if entry.record is not None and entry.record.confirmed)
        if folder.is_unassigned:
            source = folder.source_folder.name if folder.source_folder else "批量导入"
            suffix = f"{len(folder.entries)} 张 · 已确认 {confirmed} 张 · ¥{total:.2f}"
            if waiting:
                suffix += f" · 待识别 {waiting}"
            return f"待分配发票 · {source}\n{suffix}"
        application = folder.application
        label = application.application_number if application and application.application_number else folder.group_name
        detail = application.applicant if application and application.applicant else folder.group_name
        if application and application.purchase_amount is not None:
            reconciliation = reconcile_application(application, [entry.record for entry in folder.entries if entry.record])
            state = "✓" if reconciliation.matched else "差额"
            detail += f" · {state} ¥{reconciliation.difference or Decimal('0'):.2f}"
        suffix = f"{len(folder.entries)} 张 · 已确认 {confirmed} 张 · ¥{total:.2f}"
        if waiting:
            suffix += f" · 待识别 {waiting}"
        return f"{label} · {detail} · {folder.purchase_date:%Y-%m-%d}\n{suffix}"

    def _application_numbers(self, exclude_folder_id: str | None = None) -> set[str]:
        return {
            folder.application.application_number.strip()
            for folder in self.folders
            if folder.folder_id != exclude_folder_id and folder.application and folder.application.application_number.strip()
        }

    @staticmethod
    def _path_key(path: Path) -> str:
        try:
            return str(path.resolve(strict=False)).casefold()
        except OSError:
            return str(path.absolute()).casefold()

    def _imported_folder_keys(self) -> set[str]:
        return {
            self._path_key(application.source_file.parent)
            for folder in self.folders
            if (application := folder.application) is not None and application.source_file is not None
        }

    @staticmethod
    def _folder_import_summary(imported_count: int, invoice_count: int, skipped: list[tuple[Path, str]]) -> str:
        lines = [f"已导入 {imported_count} 个文件夹，共 {invoice_count} 张发票。"]
        if skipped:
            lines.append(f"已跳过 {len(skipped)} 个文件夹：")
            lines.extend(f"• {path.name or path}：{reason}" for path, reason in skipped)
        return "\n".join(lines)

    def _update_batch_summary(self) -> None:
        entries = self._all_entries()
        recognized = [entry.record for entry in entries if entry.record is not None]
        total = sum((record.total_with_tax or Decimal("0")) for record in recognized)
        confirmed = sum(1 for record in recognized if record.confirmed)
        self.batch_summary.setText(f"{len(entries)} 张发票 · 已确认 {confirmed} 张\n合计 ¥{total:,.2f}")
        self.toolbar_batch_summary.setText(
            f"{len(entries)} 张发票  ·  已确认 {confirmed} 张  ·  ¥{total:,.2f}"
        )
        self.batch_progress.setRange(0, max(1, len(entries)))
        self.batch_progress.setValue(confirmed)
        self.empty_batch_hint.setVisible(not self.folders)
        self.batch_root.setText(0, "当前批次" if self.folders else "当前批次（请先新建文件夹）")
        export_folders = [folder for folder in self.folders if folder.entries and not folder.is_unassigned]
        unassigned_entries = any(folder.is_unassigned and folder.entries for folder in self.folders)
        ready = bool(entries) and not unassigned_entries and len(recognized) == len(entries) and confirmed == len(entries) and all(
            folder.application is not None
            and folder.application.confirmed
            and validate_application(folder.application, [entry.record for entry in folder.entries if entry.record]).valid
            for folder in export_folders
        )
        if self.export_tool_button:
            self.export_tool_button.setProperty("ready", ready)
            self.export_tool_button.style().unpolish(self.export_tool_button)
            self.export_tool_button.style().polish(self.export_tool_button)
        self._update_action_availability()

    def _entry_label(self, entry: InvoiceEntry) -> str:
        if entry.error:
            state = "识别失败"
        elif entry.record is None:
            state = "识别中"
        elif entry.record.confirmed:
            state = "已确认"
        else:
            state = "待复核"
        amount = ""
        if entry.record is not None and entry.record.total_with_tax is not None:
            amount = f" · ¥{entry.record.total_with_tax:.2f}"
        return f"{entry.source_file.name}\n{state}{amount}"

    def _add_entry_item(self, folder: InvoiceFolder, entry: InvoiceEntry) -> None:
        parent = self._folder_items[folder.folder_id]
        item = QTreeWidgetItem([self._entry_label(entry)])
        item.setData(0, Qt.ItemDataRole.UserRole, ("entry", entry.entry_id))
        item.setToolTip(0, str(entry.source_file))
        item.setIcon(0, self._entry_status_icon(entry))
        parent.addChild(item)
        parent.setExpanded(True)
        self._entry_items[entry.entry_id] = item

    def _update_folder_item(self, folder: InvoiceFolder) -> None:
        item = self._folder_items.get(folder.folder_id)
        if item:
            item.setText(0, self._folder_label(folder))
            item.setToolTip(0, f"{folder.group_name} · {folder.purchase_date:%Y-%m-%d}")
        self._update_batch_summary()

    def _set_entry_text(self, entry_id: str, _message: str = "") -> None:
        found = self._entry_by_id(entry_id)
        item = self._entry_items.get(entry_id)
        if found and item:
            _, entry = found
            item.setText(0, self._entry_label(entry))
            item.setToolTip(0, str(entry.source_file))
            item.setIcon(0, self._entry_status_icon(entry))

    def _entry_status_icon(self, entry: InvoiceEntry) -> QIcon:
        if entry.error:
            return _status_icon("#FF3B30")
        if entry.record is None:
            return _status_icon("#007AFF")
        if entry.record.confirmed:
            return _status_icon("#34C759")
        return _status_icon("#FF9F0A")

    def _entry_by_id(self, entry_id: str) -> tuple[InvoiceFolder, InvoiceEntry] | None:
        for folder in self.folders:
            for entry in folder.entries:
                if entry.entry_id == entry_id:
                    return folder, entry
        return None

    def _selected_folder(self) -> InvoiceFolder | None:
        item = self.batch_tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return None
        kind, identifier = data
        if kind == "folder":
            return next((folder for folder in self.folders if folder.folder_id == identifier), None)
        if kind == "entry":
            found = self._entry_by_id(identifier)
            return found[0] if found else None
        return None

    def _update_action_availability(self) -> None:
        selected_folder = self._selected_folder()
        current = self.batch_tree.currentItem() if hasattr(self, "batch_tree") else None
        data = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        self.add_action.setEnabled(selected_folder is not None)
        self.edit_folder_action.setEnabled(bool(data and data[0] == "folder" and selected_folder and not selected_folder.is_unassigned))
        self.sidebar_more_button.setEnabled(bool(data and data[0] in {"folder", "entry"}))
        self.rename_action.setEnabled(bool(build_invoice_rename_plan(self.folders).ready_items))
        self.undo_rename_action.setEnabled(bool(self._last_rename_items))

    def _show_tree_menu(self, position: QPoint) -> None:
        item = self.batch_tree.itemAt(position)
        if item is None:
            return
        self.batch_tree.setCurrentItem(item)
        self._open_selected_menu(self.batch_tree.viewport().mapToGlobal(position))

    def _open_selected_menu(self, global_position: QPoint | None = None) -> None:
        item = self.batch_tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not data or data[0] not in {"folder", "entry"}:
            return
        menu = QMenu(self)
        if data[0] == "folder":
            edit_action = menu.addAction("修改日期和组别")
            delete_action = menu.addAction("删除空文件夹")
            chosen = menu.exec(global_position or self.sidebar_more_button.mapToGlobal(QPoint(0, self.sidebar_more_button.height())))
            if chosen is edit_action:
                self.edit_selected_folder()
            elif chosen is delete_action:
                self.delete_selected_folder()
        else:
            found = self._entry_by_id(data[1])
            assignment_actions: dict[QAction, str] = {}
            if found and found[0].is_unassigned:
                assign_menu = menu.addMenu("分配到申请单")
                targets = [folder for folder in self.folders if folder.application is not None and not folder.is_unassigned]
                if not targets:
                    assign_menu.setEnabled(False)
                for target in targets:
                    application = target.application
                    assert application is not None
                    label = application.application_number or f"{application.group_name} · {application.applicant}"
                    if application.purchase_amount is not None:
                        label += f" · ¥{application.purchase_amount:.2f}"
                    action = assign_menu.addAction(label)
                    assignment_actions[action] = target.folder_id
            remove_action = menu.addAction("移出当前批次")
            chosen = menu.exec(global_position or self.sidebar_more_button.mapToGlobal(QPoint(0, self.sidebar_more_button.height())))
            if chosen in assignment_actions:
                self.assign_selected_entry(assignment_actions[chosen])
            elif chosen is remove_action:
                self.remove_selected()

    def _current_entry_id(self) -> str | None:
        item = self.batch_tree.currentItem()
        if item is None:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        return data[1] if data and data[0] == "entry" else None

    def assign_selected_entry(self, target_folder_id: str) -> None:
        entry_id = self._current_entry_id()
        found = self._entry_by_id(entry_id) if entry_id else None
        target = self._folder_by_id(target_folder_id)
        if found is None or target is None or not found[0].is_unassigned or target.application is None:
            return
        source, entry = found
        self.save_current(entry.entry_id)
        self._move_entry_to_folder(entry, source, target)
        if not source.entries:
            self._discard_unassigned_folder(source)
        self._select_entry(entry.entry_id)
        self.statusBar().showMessage(f"已分配：{entry.source_file.name} → {target.application.application_number or target.group_name}。")

    def _receive_record(self, entry_id: str, record: InvoiceRecord) -> None:
        found = self._entry_by_id(entry_id)
        if found is None:
            return
        folder, entry = found
        if folder.application is not None:
            record.purchase_date = folder.application.purchase_date
            record.group_name = folder.application.group_name
            folder.application.difference_acknowledged = False
        else:
            record.purchase_date = folder.purchase_date
            record.group_name = folder.group_name
        entry.record = record
        entry.error = ""
        self._set_entry_text(entry_id)
        self._update_folder_item(folder)
        if entry_id == self._current_entry_id():
            self.show_entry(entry_id)

    def _receive_error(self, entry_id: str, message: str) -> None:
        found = self._entry_by_id(entry_id)
        if found is None:
            return
        folder, entry = found
        entry.error = message
        self._set_entry_text(entry_id)
        self._update_folder_item(folder)
        if entry_id == self._current_entry_id():
            self.show_entry(entry_id)

    def _recognition_finished(self, worker: RecognitionWorker) -> None:
        if worker in self.workers:
            self.workers.remove(worker)
        worker.deleteLater()
        self._finalize_pending_matches()
        if self._recognition_queue:
            self._start_queued_recognition()
            return
        self.statusBar().showMessage("识别完成。请逐张检查并确认。")

    def _finalize_pending_matches(self) -> None:
        messages: list[str] = []
        for key, pending in list(self._pending_folder_matches.items()):
            unassigned = self._folder_by_id(pending.unassigned_folder_id)
            targets = [self._folder_by_id(folder_id) for folder_id in pending.application_folder_ids]
            entries = [self._entry_by_id(entry_id) for entry_id in pending.entry_ids]
            if unassigned is None or any(folder is None for folder in targets) or any(found is None for found in entries):
                self._pending_folder_matches.pop(key, None)
                continue
            source_entries = [found[1] for found in entries if found is not None]
            if any(entry.record is None and not entry.error for entry in source_entries):
                continue

            application_folders = [folder for folder in targets if folder is not None]
            valid_entries = [entry for entry in source_entries if entry.record and entry.record.total_with_tax is not None]
            assignment = unique_invoice_assignment(
                [folder.application.purchase_amount if folder.application else None for folder in application_folders],
                [entry.record.total_with_tax if entry.record else None for entry in valid_entries],
            )
            moved = 0
            if assignment is not None:
                for target, mask in zip(application_folders, assignment, strict=True):
                    for index, entry in enumerate(valid_entries):
                        if mask & (1 << index):
                            self._move_entry_to_folder(entry, unassigned, target)
                            moved += 1

            failed = sum(1 for entry in source_entries if entry.error)
            remaining = len(unassigned.entries)
            source_name = pending.source_folder.name or str(pending.source_folder)
            if assignment is not None:
                message = f"{source_name}：已自动归属 {moved} 张发票"
                if remaining:
                    message += f"，仍有 {remaining} 张待分配"
            elif len(valid_entries) > MAX_AUTO_MATCH_INVOICES:
                message = f"{source_name}：已识别发票超过 {MAX_AUTO_MATCH_INVOICES} 张，全部保留待分配"
            elif failed:
                message = f"{source_name}：{failed} 张发票识别失败，其余发票保留待分配"
            else:
                message = f"{source_name}：金额不存在唯一匹配，{remaining} 张发票保留待分配"
            messages.append(message)
            self._pending_folder_matches.pop(key, None)
            if not unassigned.entries:
                self._discard_unassigned_folder(unassigned)
            else:
                self._update_folder_item(unassigned)

        if messages:
            QMessageBox.information(self, "发票自动归属结果", "\n".join(messages))

    def _move_entry_to_folder(self, entry: InvoiceEntry, source: InvoiceFolder, target: InvoiceFolder) -> None:
        if entry not in source.entries or target.application is None:
            return
        source.entries.remove(entry)
        target.entries.append(entry)
        if entry.record is not None:
            entry.record.purchase_date = target.application.purchase_date
            entry.record.group_name = target.application.group_name
            entry.record.confirmed = False
        target.application.confirmed = False
        target.application.difference_acknowledged = False
        item = self._entry_items.get(entry.entry_id)
        source_item = self._folder_items.get(source.folder_id)
        target_item = self._folder_items.get(target.folder_id)
        if item and source_item and target_item:
            source_item.removeChild(item)
            target_item.addChild(item)
            target_item.setExpanded(True)
            self._set_entry_text(entry.entry_id)
        self._update_folder_item(source)
        self._update_folder_item(target)
        if self._displayed_entry_id == entry.entry_id:
            self.show_entry(entry.entry_id)

    def _discard_unassigned_folder(self, folder: InvoiceFolder) -> None:
        if not folder.is_unassigned or folder.entries:
            return
        item = self._folder_items.get(folder.folder_id)
        current = self.batch_tree.currentItem()
        if item and (current is item or (current is not None and current.parent() is item)):
            self.batch_tree.setCurrentItem(self.batch_root)
        if folder in self.folders:
            self.folders.remove(folder)
        item = self._folder_items.pop(folder.folder_id, None)
        if item:
            self.batch_root.removeChild(item)
        self._update_batch_summary()

    def show_tree_item(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        data = current.data(0, Qt.ItemDataRole.UserRole) if current else None
        self._update_action_availability()
        if data and data[0] == "entry":
            self.show_entry(data[1])
            return
        if data and data[0] == "folder":
            folder = self._folder_by_id(data[1])
            if folder is not None:
                self.show_application(folder)
                return
        if self._displayed_entry_id is not None:
            self.save_current(self._displayed_entry_id)
        if self._displayed_folder_id is not None:
            self.save_application_current(self._displayed_folder_id)
        self._displayed_entry_id = None
        self._displayed_folder_id = None
        self._clear_editor("请选择文件夹中的发票，或向选中文件夹导入发票。")

    def _clear_editor(self, message: str) -> None:
        self._loading = True
        self.application_card.hide()
        self.invoice_metadata.show()
        self.table.show()
        self.table_actions_container.show()
        self.issues.show()
        self.invoice_action_bar.show()
        self.table.setRowCount(0)
        self.invoice_number.clear()
        self.total_with_tax.clear()
        self.purchase_date.setDate(QDate(2026, 1, 1))
        self.group_name.setCurrentIndex(0)
        self.confirmed.setChecked(False)
        self.issues.setPlainText(message)
        self._set_preview_message("尚未选择发票")
        self._set_editor_locked(False)
        self._set_inspector_state("empty")
        self._loading = False

    def _folder_by_id(self, folder_id: str) -> InvoiceFolder | None:
        return next((folder for folder in self.folders if folder.folder_id == folder_id), None)

    def show_application(self, folder: InvoiceFolder) -> None:
        if self._displayed_entry_id is not None:
            self.save_current(self._displayed_entry_id)
        if self._displayed_folder_id is not None and self._displayed_folder_id != folder.folder_id:
            self.save_application_current(self._displayed_folder_id)
        application = folder.application
        if application is None:
            self._show_unassigned_folder(folder)
            return
        self._loading = True
        self._displayed_entry_id = None
        self._displayed_folder_id = folder.folder_id
        self.current_file_label.setText("申请单检查器")
        self.current_context_label.setText(application.source_file.name if application.source_file else "手工建立采购单")
        self.invoice_metadata.hide()
        self.table.hide()
        self.table_actions_container.hide()
        self.issues.hide()
        self.invoice_action_bar.hide()
        self.application_card.show()
        self.application_number.setText(application.application_number)
        self.application_applicant.setText(application.applicant)
        self.application_date.setDate(_to_qdate(application.purchase_date))
        self.application_group.setCurrentIndex(max(0, self.application_group.findText(application.group_name)))
        self.application_amount.setText(_display_decimal(application.purchase_amount))
        self.application_payment_date_enabled.setChecked(application.payment_date is not None)
        self.application_payment_date.setDate(_to_qdate(application.payment_date))
        reconciliation = reconcile_application(application, [entry.record for entry in folder.entries if entry.record])
        self.application_invoice_total.setText(_display_decimal(reconciliation.invoice_total))
        self.application_difference.setText("" if reconciliation.difference is None else _display_decimal(reconciliation.difference))
        self.application_difference_acknowledged.setChecked(application.difference_acknowledged)
        self.application_difference_acknowledged.setEnabled(not reconciliation.matched)
        self.application_open_source.setEnabled(application.source_file is not None and application.source_file.exists())
        self.application_confirm_button.setText("已确认" if application.confirmed else "确认申请单信息")
        self.application_confirm_button.setEnabled(not application.confirmed)
        self._show_application_preview(application)
        self._set_status_badge("✓ 已确认" if application.confirmed else "待复核", "success" if application.confirmed else "pending")
        self._loading = False

    def _show_unassigned_folder(self, folder: InvoiceFolder) -> None:
        self._displayed_entry_id = None
        self._displayed_folder_id = None
        self.current_file_label.setText("待分配发票")
        source = folder.source_folder.name if folder.source_folder else "批量导入"
        self.current_context_label.setText(source)
        self._clear_editor("这些发票尚未匹配到唯一申请单。请右键某张发票，选择“分配到申请单”。")
        self._set_preview_message(f"待分配发票 · {source}\n\n自动匹配不唯一或未匹配时，需手工指定申请单。")

    def _show_application_preview(self, application: PurchaseApplication) -> None:
        source = str(application.source_file) if application.source_file else "手工录入"
        self._set_preview_message(f"申请表已本地读取\n{source}\n\n请在右侧确认申请单信息")

    def save_application_current(self, folder_id: str | None = None) -> None:
        if self._loading:
            return
        folder = self._folder_by_id(folder_id or self._displayed_folder_id or "")
        if folder is None or folder.application is None:
            return
        application = folder.application
        old_number = application.application_number
        old_date, old_group = application.purchase_date, application.group_name
        entered_number = self.application_number.text().strip()
        application.applicant = self.application_applicant.text().strip()
        selected = self.application_date.date()
        application.purchase_date = date(selected.year(), selected.month(), selected.day())
        application.group_name = "" if self.application_group.currentIndex() == 0 else self.application_group.currentText()
        application.purchase_amount = to_decimal(self.application_amount.text())
        application.payment_date = None
        if self.application_payment_date_enabled.isChecked():
            paid = self.application_payment_date.date()
            application.payment_date = date(paid.year(), paid.month(), paid.day())
        application.difference_acknowledged = self.application_difference_acknowledged.isChecked()
        if application.number_is_automatic and entered_number == old_number and (old_date != application.purchase_date or old_group != application.group_name):
            application.application_number = generate_application_number(
                application.group_name, application.purchase_date, self._application_numbers(exclude_folder_id=folder.folder_id)
            )
            self._loading = True
            self.application_number.setText(application.application_number)
            self._loading = False
        else:
            application.application_number = entered_number
            if entered_number != old_number:
                application.number_is_automatic = False
        folder.purchase_date = application.purchase_date
        folder.group_name = application.group_name
        for entry in folder.entries:
            if entry.record is not None:
                entry.record.purchase_date = application.purchase_date
                entry.record.group_name = application.group_name
        self._update_folder_item(folder)

    def mark_application_unconfirmed(self) -> None:
        if self._loading:
            return
        folder = self._folder_by_id(self._displayed_folder_id or "")
        if folder is None or folder.application is None:
            return
        folder.application.confirmed = False
        folder.application.difference_acknowledged = False
        self.application_difference_acknowledged.setChecked(False)
        self.application_confirm_button.setText("确认申请单信息")
        self.application_confirm_button.setEnabled(True)
        self._update_folder_item(folder)

    def confirm_application_current(self) -> None:
        folder = self._folder_by_id(self._displayed_folder_id or "")
        if folder is None or folder.application is None:
            return
        self.save_application_current(folder.folder_id)
        duplicate = folder.application.application_number.strip() in self._application_numbers(exclude_folder_id=folder.folder_id)
        result = validate_application(folder.application, [entry.record for entry in folder.entries if entry.record], duplicate_number=duplicate)
        if not result.valid:
            QMessageBox.warning(self, "不能确认", "；".join(issue.message for issue in result.errors))
            return
        folder.application.confirmed = True
        self._update_folder_item(folder)
        self.show_application(folder)

    def open_application_source(self) -> None:
        folder = self._folder_by_id(self._displayed_folder_id or "")
        if folder and folder.application and folder.application.source_file:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.application.source_file)))

    def show_entry(self, entry_id: str) -> None:
        if self._displayed_folder_id is not None:
            self.save_application_current(self._displayed_folder_id)
        if self._displayed_entry_id is not None and self._displayed_entry_id != entry_id:
            self.save_current(self._displayed_entry_id)
        found = self._entry_by_id(entry_id)
        if found is None:
            self._displayed_entry_id = None
            self._clear_editor("发票已被移除。")
            return
        folder, entry = found
        self._loading = True
        self._displayed_folder_id = None
        self.application_card.hide()
        self.invoice_metadata.show()
        self.table.show()
        self.table_actions_container.show()
        self.issues.show()
        self.invoice_action_bar.show()
        self.table.setRowCount(0)
        self.invoice_number.clear()
        self.total_with_tax.clear()
        self.purchase_date.setDate(QDate(2026, 1, 1))
        self.group_name.setCurrentIndex(0)
        self.confirmed.setChecked(False)
        self.issues.clear()
        self._displayed_entry_id = entry_id
        self.current_file_label.setText(entry.source_file.name)
        self.current_context_label.setText(f"{folder.group_name} · {folder.purchase_date:%Y-%m-%d}")
        if entry.error:
            self.issues.set_state("error", "识别失败", [(entry.error, "", None)])
            self._set_preview_message("无法预览此发票")
            self._set_editor_locked(True)
            self._set_inspector_state("error")
            self._loading = False
            return
        record = entry.record
        if record is None:
            self.issues.set_state("neutral", "正在识别，请稍候")
            self._set_preview_message("正在识别")
            self._set_editor_locked(True)
            self._set_inspector_state("working")
            self._loading = False
            return
        self.invoice_number.setText(record.invoice_number)
        self.total_with_tax.setText(_display_decimal(record.total_with_tax))
        self.purchase_date.setDate(_to_qdate(record.purchase_date))
        self.group_name.setCurrentIndex(max(0, self.group_name.findText(record.group_name)))
        self.confirmed.setChecked(record.confirmed)
        for item in record.items:
            self._append_item(item)
        self._show_preview(record.preview_path)
        self._loading = False
        self._set_editor_locked(record.confirmed)
        if folder.application is not None and folder.application.source_file is not None:
            self.purchase_date.setEnabled(False)
            self.group_name.setEnabled(False)
        self._set_inspector_state("unassigned" if folder.is_unassigned else ("confirmed" if record.confirmed else "pending"))
        self.refresh_issues()

    def _set_status_badge(self, text: str, state: str) -> None:
        self.invoice_status_badge.setText(text)
        self.invoice_status_badge.setProperty("state", state)
        self.invoice_status_badge.style().unpolish(self.invoice_status_badge)
        self.invoice_status_badge.style().polish(self.invoice_status_badge)

    def _set_inspector_state(self, state: str) -> None:
        if state == "confirmed":
            self._set_status_badge("✓ 已确认", "success")
            self.action_state_label.setText("内容已锁定")
            self.confirm_button.hide()
            self.cancel_confirm_button.show()
            self.confirm_action.setEnabled(False)
            self.cancel_confirm_action.setEnabled(True)
        elif state == "pending":
            self._set_status_badge("待复核", "pending")
            self.action_state_label.setText("核对无误后确认")
            self.confirm_button.show()
            self.cancel_confirm_button.hide()
            self.confirm_action.setEnabled(True)
            self.cancel_confirm_action.setEnabled(False)
        elif state == "working":
            self._set_status_badge("识别中", "working")
            self.action_state_label.setText("正在本地识别")
            self.confirm_button.hide()
            self.cancel_confirm_button.hide()
            self.confirm_action.setEnabled(False)
            self.cancel_confirm_action.setEnabled(False)
        elif state == "error":
            self._set_status_badge("识别失败", "error")
            self.action_state_label.setText("请移出后重新导入清晰文件")
            self.confirm_button.hide()
            self.cancel_confirm_button.hide()
            self.confirm_action.setEnabled(False)
            self.cancel_confirm_action.setEnabled(False)
        elif state == "unassigned":
            self._set_status_badge("待分配", "pending")
            self.action_state_label.setText("请右键分配到申请单")
            self.confirm_button.hide()
            self.cancel_confirm_button.hide()
            self.confirm_action.setEnabled(False)
            self.cancel_confirm_action.setEnabled(False)
        else:
            self.current_file_label.setText("发票检查器")
            self.current_context_label.setText("从左侧选择一张发票")
            self._set_status_badge("未选择", "neutral")
            self.action_state_label.setText("请选择一张发票")
            self.confirm_button.hide()
            self.cancel_confirm_button.hide()
            self.confirm_action.setEnabled(False)
            self.cancel_confirm_action.setEnabled(False)

    def _append_item(self, item: InvoiceItem | None = None) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        values = [
            item.project_name if item else "",
            item.specification if item else "",
            item.unit if item else "",
            _display_decimal(item.quantity) if item else "",
            _display_decimal(item.unit_price) if item else "",
            _display_decimal(item.amount) if item else "",
            _display_percent(item.tax_rate) if item else "",
            _display_decimal(item.tax_amount) if item else "",
        ]
        for column, value in enumerate(values):
            cell = QTableWidgetItem(value)
            if item and item.confidence.get(ITEM_FIELDS[column], 1.0) < 0.90:
                cell.setBackground(QColor("#FFF2D8"))
            if column >= 3:
                cell.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, column, cell)

    def _show_preview(self, path: Path | None) -> None:
        if not path or not path.exists():
            self._set_preview_message("预览不可用")
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._set_preview_message("预览不可用")
            return
        self._preview_source = pixmap
        self._preview_zoom = 1.0
        self._rescale_preview()

    def _set_preview_message(self, message: str) -> None:
        self._preview_source = None
        self._preview_zoom = 1.0
        self.zoom_label.setText("100%")
        self.preview.setPixmap(QPixmap())
        self.preview.setText(message)
        available = self.preview_scroll.viewport().size()
        self.preview_canvas.resize(max(1, available.width()), max(1, available.height()))
        self.preview.resize(self.preview_canvas.size())
        self.preview.move(0, 0)

    def _rescale_preview(self) -> None:
        if self._preview_source is None:
            return
        available = self.preview_scroll.viewport().size()
        if available.width() <= 1 or available.height() <= 1:
            return
        base_scale = min(available.width() / self._preview_source.width(), available.height() / self._preview_source.height())
        target_size = QSize(
            max(1, round(self._preview_source.width() * base_scale * self._preview_zoom)),
            max(1, round(self._preview_source.height() * base_scale * self._preview_zoom)),
        )
        scaled = self._preview_source.scaled(target_size, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self.preview.setText("")
        self.preview.setPixmap(scaled)
        self.preview.resize(scaled.size())
        canvas_size = QSize(max(available.width(), scaled.width()), max(available.height(), scaled.height()))
        self.preview_canvas.resize(canvas_size)
        self.preview.move((canvas_size.width() - scaled.width()) // 2, (canvas_size.height() - scaled.height()) // 2)
        self.zoom_label.setText(f"{round(self._preview_zoom * 100)}%")

    def zoom_in(self) -> None:
        self.zoom_at(1.25, self.preview_scroll.viewport().rect().center())

    def zoom_out(self) -> None:
        self.zoom_at(0.8, self.preview_scroll.viewport().rect().center())

    def zoom_at(self, factor: float, viewport_position: QPoint) -> None:
        if self._preview_source is None:
            return
        old_size = self.preview.size()
        image_position = self.preview.mapFrom(self.preview_scroll.viewport(), viewport_position)
        ratio_x = min(1.0, max(0.0, image_position.x() / max(1, old_size.width())))
        ratio_y = min(1.0, max(0.0, image_position.y() / max(1, old_size.height())))
        self._preview_zoom = min(4.0, max(0.5, self._preview_zoom * factor))
        self._rescale_preview()
        QTimer.singleShot(0, lambda: self._restore_zoom_anchor(ratio_x, ratio_y, viewport_position))

    def _restore_zoom_anchor(self, ratio_x: float, ratio_y: float, viewport_position: QPoint) -> None:
        available = self.preview_scroll.viewport().size()
        image_x = self._image_axis_position(self.preview.width(), available.width(), ratio_x, viewport_position.x())
        image_y = self._image_axis_position(self.preview.height(), available.height(), ratio_y, viewport_position.y())
        self.preview.move(image_x, image_y)
        self.preview_scroll.horizontalScrollBar().setValue(
            round(self.preview.width() * ratio_x + image_x - viewport_position.x())
        )
        self.preview_scroll.verticalScrollBar().setValue(
            round(self.preview.height() * ratio_y + image_y - viewport_position.y())
        )

    @staticmethod
    def _image_axis_position(image_size: int, viewport_size: int, ratio: float, cursor: int) -> int:
        """Return the image origin that keeps a visible cursor point stable."""
        if image_size >= viewport_size:
            return 0
        desired = round(cursor - image_size * ratio)
        return min(max(0, desired), viewport_size - image_size)

    def zoom_fit(self) -> None:
        self._preview_zoom = 1.0
        self._rescale_preview()
        self.preview_scroll.horizontalScrollBar().setValue(0)
        self.preview_scroll.verticalScrollBar().setValue(0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._rescale_preview()
        if hasattr(self, "drop_overlay"):
            margin = 24
            viewport = self.preview_scroll.viewport().rect()
            self.drop_overlay.setGeometry(viewport.adjusted(margin, margin, -margin, -margin))

    def _set_editor_locked(self, locked: bool) -> None:
        """Confirmed invoices are view-only until the user explicitly cancels."""
        self.invoice_number.setReadOnly(locked)
        self.total_with_tax.setReadOnly(locked)
        self.purchase_date.setEnabled(not locked)
        self.group_name.setEnabled(not locked)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers if locked else self._table_edit_triggers
        )
        self.add_item_button.setEnabled(not locked)
        self.remove_item_button.setEnabled(not locked)
        self.confirm_button.setEnabled(not locked)
        self.confirm_action.setEnabled(not locked)
        self.cancel_confirm_button.setEnabled(locked)
        self.cancel_confirm_action.setEnabled(locked)

    def save_current(self, entry_id: str | None = None) -> None:
        if self._loading:
            return
        if entry_id is None:
            entry_id = self._displayed_entry_id
        if entry_id is None:
            return
        found = self._entry_by_id(entry_id)
        if found is None or found[1].record is None:
            return
        folder, entry = found
        record = entry.record
        record.invoice_number = self.invoice_number.text().strip()
        record.total_with_tax = to_decimal(self.total_with_tax.text())
        selected_date = self.purchase_date.date()
        record.purchase_date = date(selected_date.year(), selected_date.month(), selected_date.day())
        record.group_name = "" if self.group_name.currentIndex() == 0 else self.group_name.currentText()
        previous_items = record.items
        record.items = []
        for row in range(self.table.rowCount()):
            values = [self.table.item(row, column).text().strip() if self.table.item(row, column) else "" for column in range(8)]
            previous = previous_items[row] if row < len(previous_items) else None
            record.items.append(
                InvoiceItem(
                    project_name=values[0],
                    specification=values[1],
                    unit=values[2],
                    quantity=to_decimal(values[3]),
                    unit_price=to_decimal(values[4]),
                    amount=to_decimal(values[5]),
                    tax_rate=to_decimal(values[6]),
                    tax_amount=to_decimal(values[7]),
                    confidence=previous.confidence.copy() if previous else {},
                    boxes=previous.boxes.copy() if previous else {},
                )
            )
        record.confirmed = self.confirmed.isChecked()
        self._update_folder_item(folder)

    def refresh_issues(self) -> bool:
        self.save_current()
        entry_id = self._displayed_entry_id
        found = self._entry_by_id(entry_id) if entry_id else None
        if found is None or found[1].record is None:
            return False
        record = found[1].record
        result = validate_invoice(record)
        records = self._all_records()
        duplicate = validate_duplicates(records)
        issues = list(result.issues)
        if duplicate:
            source_index = records.index(record)
            if source_index in duplicate:
                issues.append(duplicate[source_index])
        issue_rows = [(issue.message, issue.field, issue.item_index) for issue in issues]
        if issue_rows:
            self.issues.set_state("error", f"发现 {len(issue_rows)} 个需要处理的问题", issue_rows)
        else:
            self.issues.set_state("success", "✓  金额核对通过")
        self._highlight_issues(result)
        return result.valid and not duplicate

    def _highlight_issues(self, result) -> None:
        for row in range(self.table.rowCount()):
            for column in range(self.table.columnCount()):
                cell = self.table.item(row, column)
                if cell:
                    cell.setBackground(QColor("#FFFFFF" if row % 2 == 0 else "#FAFAFC"))
        found = self._entry_by_id(self._displayed_entry_id) if self._displayed_entry_id else None
        record = found[1].record if found else None
        if record is not None:
            for row, item in enumerate(record.items):
                for column, field in enumerate(ITEM_FIELDS):
                    cell = self.table.item(row, column)
                    if cell and item.confidence.get(field, 1.0) < 0.90:
                        cell.setBackground(QColor("#FFF2D8"))
        self.invoice_number.setStyleSheet("")
        self.total_with_tax.setStyleSheet("")
        for issue in result.issues:
            if issue.field == "invoice_number":
                self.invoice_number.setStyleSheet("background: #FFF4F3; border: 2px solid #FF8A80;")
            elif issue.field == "total_with_tax":
                self.total_with_tax.setStyleSheet("background: #FFF4F3; border: 2px solid #FF8A80;")
            elif issue.item_index is not None and issue.field in ITEM_FIELDS:
                column = ITEM_FIELDS.index(issue.field)
                cell = self.table.item(issue.item_index, column)
                if cell:
                    cell.setBackground(QColor("#FFE9E7"))

    def _focus_issue(self, field: str, item_index: int) -> None:
        if field == "invoice_number":
            self.invoice_number.setFocus()
            self.invoice_number.selectAll()
            return
        if field == "total_with_tax":
            self.total_with_tax.setFocus()
            self.total_with_tax.selectAll()
            return
        if item_index >= 0 and field in ITEM_FIELDS and item_index < self.table.rowCount():
            column = ITEM_FIELDS.index(field)
            self.table.setCurrentCell(item_index, column)
            self.table.scrollToItem(self.table.item(item_index, column))
            self.table.setFocus()

    def confirm_current(self) -> None:
        current_id = self._displayed_entry_id
        found = self._entry_by_id(current_id) if current_id else None
        if found and found[0].is_unassigned:
            self.statusBar().showMessage("请先右键将发票分配到申请单，再确认。", 5000)
            return
        self.save_current()
        if self.refresh_issues():
            entry_id = self._displayed_entry_id
            found = self._entry_by_id(entry_id) if entry_id else None
            if found is None or found[1].record is None:
                return
            folder, entry = found
            record = entry.record
            record.confirmed = True
            self.confirmed.setChecked(True)
            self._set_entry_text(entry.entry_id)
            self._set_editor_locked(True)
            self._set_inspector_state("confirmed")
            self._update_folder_item(folder)
            next_entry = self._next_unconfirmed_entry(folder, entry)
            if next_entry is not None:
                QTimer.singleShot(0, lambda: self._select_entry(next_entry.entry_id))
                self.statusBar().showMessage("当前发票已确认，已切换到下一张。")
            else:
                self.statusBar().showMessage("当前发票已确认，本批次已无待确认发票。")
        else:
            self.statusBar().showMessage("尚不能确认：请处理校验卡中标出的字段。", 5000)
            if self.issues.list.count():
                self._focus_issue(*self.issues.list.item(0).data(Qt.ItemDataRole.UserRole))

    def cancel_confirmation(self) -> None:
        entry_id = self._displayed_entry_id
        found = self._entry_by_id(entry_id) if entry_id else None
        if found is None or found[1].record is None:
            self.statusBar().showMessage("请先选择一张已识别的发票。")
            return
        folder, entry = found
        if not entry.record.confirmed:
            self.statusBar().showMessage("当前发票尚未确认。")
            return
        entry.record.confirmed = False
        self.confirmed.setChecked(False)
        self._set_editor_locked(False)
        self._set_entry_text(entry.entry_id)
        self._set_inspector_state("pending")
        self._update_folder_item(folder)
        self.statusBar().showMessage("已取消确认，可继续修改当前发票。")

    def preview_invoice_renames(self) -> None:
        """Show every batch entry and require an explicit confirmation to rename."""
        self.save_current()
        plan = build_invoice_rename_plan(self.folders)
        if not plan.ready_items:
            QMessageBox.information(self, "没有可重命名的发票", "当前批次没有已确认且可安全重命名的发票。")
            return
        dialog = RenamePreviewDialog(plan.items, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        result = rename_invoice_files(plan.ready_items)
        if not result.success:
            QMessageBox.critical(self, "重命名失败", result.error)
            return
        self._last_rename_items = plan.ready_items
        self._apply_invoice_rename_paths(plan.ready_items)
        self._update_batch_summary()
        QMessageBox.information(self, "重命名完成", f"已重命名 {len(plan.ready_items)} 张发票。")
        self.statusBar().showMessage(f"已重命名 {len(plan.ready_items)} 张发票，可使用“撤销上次重命名”恢复。")

    def undo_last_invoice_rename(self) -> None:
        if not self._last_rename_items:
            QMessageBox.information(self, "没有可撤销的操作", "本次运行中还没有成功的批量重命名。")
            return
        result = undo_invoice_renames(self._last_rename_items)
        if not result.success:
            QMessageBox.critical(self, "撤销失败", result.error)
            return
        self._apply_invoice_rename_paths(self._last_rename_items, undo=True)
        count = len(self._last_rename_items)
        self._last_rename_items = ()
        self._update_batch_summary()
        QMessageBox.information(self, "已撤销", f"已恢复 {count} 张发票的原文件名。")
        self.statusBar().showMessage(f"已恢复 {count} 张发票的原文件名。")

    def _apply_invoice_rename_paths(self, items: tuple[InvoiceRenameItem, ...], *, undo: bool = False) -> None:
        """Synchronize in-memory source and image-preview paths after a file rename."""
        updated_folders: set[str] = set()
        for item in items:
            found = self._entry_by_id(item.entry_id)
            if found is None or item.target is None:
                continue
            folder, entry = found
            old_path, new_path = (item.target, item.source) if undo else (item.source, item.target)
            entry.source_file = new_path
            if entry.record is not None:
                entry.record.source_file = new_path
                if entry.record.preview_path is not None and _same_path(entry.record.preview_path, old_path):
                    entry.record.preview_path = new_path
            self._set_entry_text(entry.entry_id)
            updated_folders.add(folder.folder_id)
        for folder_id in updated_folders:
            folder = self._folder_by_id(folder_id)
            if folder is not None:
                self._update_folder_item(folder)
        if self._displayed_entry_id:
            self.show_entry(self._displayed_entry_id)

    def export_current_batch(self) -> None:
        self.save_current()
        self.save_application_current()
        unassigned_count = sum(len(folder.entries) for folder in self.folders if folder.is_unassigned)
        if unassigned_count:
            QMessageBox.warning(self, "不能导出", f"仍有 {unassigned_count} 张待分配发票，请先右键分配到申请单。")
            return
        export_folders = [folder for folder in self.folders if folder.entries and not folder.is_unassigned]
        entries = [entry for folder in export_folders for entry in folder.entries]
        records = [entry.record for entry in entries if entry.record is not None]
        if not records or len(records) != len(entries):
            QMessageBox.warning(self, "不能导出", "存在尚未识别成功的发票。")
            return
        if not all(record.confirmed for record in records):
            QMessageBox.warning(self, "不能导出", "请先人工确认每一张发票。")
            return
        if validate_duplicates(records):
            QMessageBox.warning(self, "不能导出", "同一批次有重复发票号码。")
            return
        numbers = {
            folder.application.application_number.strip()
            for folder in export_folders
            if folder.application and folder.application.application_number.strip()
        }
        if len(numbers) != len([folder for folder in export_folders if folder.application and folder.application.application_number.strip()]):
            QMessageBox.warning(self, "不能导出", "同一批次有重复申请单编号。")
            return
        for folder in export_folders:
            result = validate_application(folder.application, [entry.record for entry in folder.entries if entry.record])
            if not result.valid or not folder.application or not folder.application.confirmed:
                message = "；".join(issue.message for issue in result.errors) if result.errors else "请先确认申请单信息。"
                QMessageBox.warning(self, "不能导出", message)
                return
        selected, _ = QFileDialog.getSaveFileName(self, "导出 Excel", default_output_name(), "Excel 文件 (*.xlsx)")
        if not selected:
            return
        if not selected.lower().endswith(".xlsx"):
            selected += ".xlsx"
        try:
            output = export_excel(export_folders, selected)
        except Exception as error:
            QMessageBox.critical(self, "导出失败", str(error))
            return
        QMessageBox.information(self, "导出完成", f"已生成：\n{output}")
        self.statusBar().showMessage(f"已导出：{output}")

    def add_item_row(self) -> None:
        self._append_item()
        self.mark_unconfirmed()

    def remove_item_row(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self.table.removeRow(row)
            self.mark_unconfirmed()

    def remove_selected(self) -> None:
        entry_id = self._current_entry_id()
        if entry_id is None:
            QMessageBox.warning(self, "请选择发票", "请在文件夹中选择要移除的发票。")
            return
        self.save_current()
        found = self._entry_by_id(entry_id)
        if found is None:
            return
        folder, entry = found
        folder.entries.remove(entry)
        if folder.application is not None:
            folder.application.confirmed = False
            folder.application.difference_acknowledged = False
        item = self._entry_items.pop(entry_id, None)
        if item:
            folder_item = self._folder_items.get(folder.folder_id)
            if folder_item:
                folder_item.removeChild(item)
        self._update_folder_item(folder)
        if self._displayed_entry_id == entry_id:
            self._displayed_entry_id = None
        self.batch_tree.setCurrentItem(self._folder_items[folder.folder_id])
        self.statusBar().showMessage(f"已移除：{entry.source_file.name}。原始文件未被删除。")

    def delete_selected_folder(self) -> None:
        item = self.batch_tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not data or data[0] != "folder":
            QMessageBox.warning(self, "请选择文件夹", "请先选择要删除的空文件夹。")
            return
        folder = next((value for value in self.folders if value.folder_id == data[1]), None)
        if folder is None:
            return
        if folder.entries:
            QMessageBox.warning(self, "不能删除", "文件夹中仍有发票，请先移除其中的发票。")
            return
        self.folders.remove(folder)
        folder_item = self._folder_items.pop(folder.folder_id, None)
        if folder_item:
            self.batch_root.removeChild(folder_item)
        self._update_batch_summary()
        self.batch_tree.setCurrentItem(self.batch_root)
        self.statusBar().showMessage("已删除空文件夹。")

    def _all_entries(self) -> list[InvoiceEntry]:
        return [entry for folder in self.folders for entry in folder.entries]

    def _all_records(self) -> list[InvoiceRecord]:
        return [entry.record for entry in self._all_entries() if entry.record is not None]

    def _next_unconfirmed_entry(self, folder: InvoiceFolder, entry: InvoiceEntry) -> InvoiceEntry | None:
        folder_index = self.folders.index(folder)
        entry_index = folder.entries.index(entry)
        candidates = folder.entries[entry_index + 1 :]
        for later_folder in self.folders[folder_index + 1 :]:
            if not later_folder.is_unassigned:
                candidates.extend(later_folder.entries)
        return next((candidate for candidate in candidates if candidate.record is None or not candidate.record.confirmed), None)

    def _select_entry(self, entry_id: str) -> None:
        item = self._entry_items.get(entry_id)
        if item:
            self.batch_tree.setCurrentItem(item)

    def mark_unconfirmed(self) -> None:
        if self._loading:
            return
        found = self._entry_by_id(self._displayed_entry_id) if self._displayed_entry_id else None
        if found and found[1].record is not None:
            folder, entry = found
            if folder.application is not None:
                folder.application.confirmed = False
                folder.application.difference_acknowledged = False
            entry = found[1]
            if entry.record.confirmed:
                return
            entry.record.confirmed = False
            self.confirmed.setChecked(False)
            self._set_entry_text(entry.entry_id)
            self._set_inspector_state("pending")

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
            folders = [path for path in paths if path.is_dir()]
            if folders and len(folders) != len(paths):
                self.drop_overlay.setText("请不要混合拖入文件和文件夹")
            elif folders:
                self.drop_overlay.setText(f"松开以批量导入\n{len(folders)} 个资料文件夹")
            else:
                folder = self._selected_folder()
                if folder is None:
                    self.drop_overlay.setText("请先选择一个日期分组")
                else:
                    self.drop_overlay.setText(
                        f"松开以导入到\n{folder.group_name} · {folder.purchase_date:%Y-%m-%d}"
                    )
            self.drop_overlay.show()
            self.drop_overlay.raise_()
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self.drop_overlay.hide()
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self.drop_overlay.hide()
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        folders = [path for path in paths if path.is_dir()]
        if folders and len(folders) != len(paths):
            QMessageBox.warning(self, "请分开导入", "请不要混合拖入文件和文件夹；请分两次操作。")
        elif folders:
            self.import_folders(folders)
        else:
            self.add_files(paths)
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        self.save_current()
        self.batch_tree.blockSignals(True)
        self._recognition_queue.clear()
        for worker in self.workers:
            if worker.isRunning():
                worker.quit()
                worker.wait(1500)
        self.service.cleanup()
        event.accept()


def _display_decimal(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


def _same_path(left: Path, right: Path) -> bool:
    return path_key(left) == path_key(right)


def _display_percent(value: Decimal | None) -> str:
    return "" if value is None else f"{value * Decimal('100'):f}%"


def _to_qdate(value: date | None) -> QDate:
    if value is None:
        return QDate(2026, 1, 1)
    return QDate(value.year, value.month, value.day)
