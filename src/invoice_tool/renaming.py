"""Safe, local batch renaming for confirmed invoice source files."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal
from uuid import uuid4

from .applications import GROUP_PREFIXES, normalize_group
from .models import InvoiceFolder
from .service import SUPPORTED_EXTENSIONS

RenameStatus = Literal["ready", "unchanged", "skipped", "conflict"]


@dataclass(frozen=True, slots=True)
class InvoiceRenameItem:
    """One evaluated source file in a rename preview."""

    entry_id: str
    source: Path
    target: Path | None
    status: RenameStatus
    reason: str = ""


@dataclass(frozen=True, slots=True)
class InvoiceRenamePlan:
    items: tuple[InvoiceRenameItem, ...]

    @property
    def ready_items(self) -> tuple[InvoiceRenameItem, ...]:
        return tuple(item for item in self.items if item.status == "ready")


@dataclass(frozen=True, slots=True)
class RenameExecutionResult:
    success: bool
    items: tuple[InvoiceRenameItem, ...]
    error: str = ""


def path_key(path: Path) -> str:
    """A Windows-safe, case-insensitive key for file-path comparisons."""
    return os.path.normcase(os.path.abspath(path))


def build_invoice_rename_plan(folders: Iterable[InvoiceFolder]) -> InvoiceRenamePlan:
    """Build a non-mutating rename plan for every entry in the current batch."""
    items: list[InvoiceRenameItem] = []
    for folder in folders:
        for entry in folder.entries:
            source = entry.source_file
            record = entry.record
            if folder.is_unassigned:
                items.append(InvoiceRenameItem(entry.entry_id, source, None, "skipped", "待分配发票不能重命名。"))
                continue
            if record is None or entry.error:
                items.append(InvoiceRenameItem(entry.entry_id, source, None, "skipped", "发票尚未识别成功。"))
                continue
            if not record.confirmed:
                items.append(InvoiceRenameItem(entry.entry_id, source, None, "skipped", "发票尚未人工确认。"))
                continue
            prefix = GROUP_PREFIXES.get(normalize_group(record.group_name or folder.group_name), "")
            if not prefix:
                items.append(InvoiceRenameItem(entry.entry_id, source, None, "skipped", "缺少可用的组别前缀。"))
                continue
            number = record.invoice_number.strip()
            if not number.isdigit() or not 15 <= len(number) <= 25:
                items.append(InvoiceRenameItem(entry.entry_id, source, None, "skipped", "发票号码不是 15 至 25 位数字。"))
                continue
            if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
                items.append(InvoiceRenameItem(entry.entry_id, source, None, "skipped", "不支持的发票文件类型。"))
                continue
            if not source.is_file():
                items.append(InvoiceRenameItem(entry.entry_id, source, None, "skipped", "原文件不存在或无法访问。"))
                continue
            target = source.with_name(f"{prefix}-{number}{source.suffix}")
            if path_key(source) == path_key(target):
                items.append(InvoiceRenameItem(entry.entry_id, source, target, "unchanged", "文件名已符合规范。"))
            else:
                items.append(InvoiceRenameItem(entry.entry_id, source, target, "ready", ""))

    return InvoiceRenamePlan(tuple(_mark_conflicts(items)))


def _mark_conflicts(items: list[InvoiceRenameItem]) -> list[InvoiceRenameItem]:
    """Mark duplicate targets and occupied destinations without changing files."""
    result = list(items)
    target_groups: dict[str, list[int]] = {}
    for index, item in enumerate(result):
        if item.status == "ready" and item.target is not None:
            target_groups.setdefault(path_key(item.target), []).append(index)
    for indexes in target_groups.values():
        if len(indexes) > 1:
            for index in indexes:
                item = result[index]
                result[index] = InvoiceRenameItem(item.entry_id, item.source, item.target, "conflict", "同一文件夹内有重复目标文件名。")

    # A target occupied by another ready source is safe: execution first moves
    # every source to a unique temporary name. Re-evaluate until all otherwise
    # occupied targets are marked, including dependencies on a new conflict.
    changed = True
    while changed:
        changed = False
        ready_sources = {path_key(item.source) for item in result if item.status == "ready"}
        for index, item in enumerate(result):
            if item.status != "ready" or item.target is None:
                continue
            if item.target.exists() and path_key(item.target) not in ready_sources:
                result[index] = InvoiceRenameItem(item.entry_id, item.source, item.target, "conflict", "目标文件名已被其他文件占用。")
                changed = True
    return result


def rename_invoice_files(items: Iterable[InvoiceRenameItem]) -> RenameExecutionResult:
    """Rename ready plan items atomically as far as the local filesystem allows."""
    ready = tuple(item for item in items if item.status == "ready" and item.target is not None)
    return _perform_rename(ready)


def undo_invoice_renames(items: Iterable[InvoiceRenameItem]) -> RenameExecutionResult:
    """Reverse one successful batch, without overwriting files created afterwards."""
    reverse = tuple(
        InvoiceRenameItem(item.entry_id, item.target, item.source, "ready")
        for item in items
        if item.status == "ready" and item.target is not None
    )
    return _perform_rename(reverse)


def _perform_rename(items: tuple[InvoiceRenameItem, ...]) -> RenameExecutionResult:
    if not items:
        return RenameExecutionResult(True, items)
    source_keys = {path_key(item.source) for item in items}
    target_keys = [path_key(item.target) for item in items if item.target is not None]
    if len(target_keys) != len(set(target_keys)):
        return RenameExecutionResult(False, items, "目标文件名重复，未执行重命名。")
    for item in items:
        assert item.target is not None
        if not item.source.is_file():
            return RenameExecutionResult(False, items, f"文件不存在或无法访问：{item.source.name}")
        if item.target.exists() and path_key(item.target) not in source_keys:
            return RenameExecutionResult(False, items, f"目标文件名已被占用：{item.target.name}")

    staged: list[tuple[InvoiceRenameItem, Path]] = []
    completed: list[InvoiceRenameItem] = []
    try:
        for item in items:
            temp = _temporary_path(item.source)
            item.source.rename(temp)
            staged.append((item, temp))
        for item, temp in staged:
            assert item.target is not None
            temp.rename(item.target)
            completed.append(item)
    except OSError as error:
        rollback_errors: list[str] = []
        for item in reversed(completed):
            assert item.target is not None
            try:
                if item.target.exists() and not item.source.exists():
                    item.target.rename(item.source)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        for item, temp in reversed(staged):
            try:
                if temp.exists() and not item.source.exists():
                    temp.rename(item.source)
            except OSError as rollback_error:
                rollback_errors.append(str(rollback_error))
        suffix = "；回滚时出现问题：" + "；".join(rollback_errors) if rollback_errors else ""
        return RenameExecutionResult(False, items, f"重命名失败：{error}{suffix}")
    return RenameExecutionResult(True, items)


def _temporary_path(source: Path) -> Path:
    while True:
        candidate = source.with_name(f".{source.stem}.invoice-renaming-{uuid4().hex}{source.suffix}")
        if not candidate.exists():
            return candidate
