from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from .service import InvoiceService
from .ui import MainWindow
from .validation import validate_invoice


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--self-test":
        return _self_test(sys.argv[2:])
    app = QApplication(sys.argv)
    app.setApplicationName("本地发票识别工具")
    window = MainWindow(sys.argv[1:])
    window.show()
    return app.exec()


def _self_test(paths: list[str]) -> int:
    """供打包验收使用：不启动界面，离线解析传入的样张并验证金额。"""
    service = InvoiceService()
    try:
        for path in paths:
            record = service.parse_file(path)
            result = validate_invoice(record)
            if not result.valid:
                raise ValueError("；".join(issue.message for issue in result.errors))
            print(f"OK {record.invoice_number} {len(record.items)} {record.total_with_tax}")
        return 0
    except Exception as error:
        print(f"SELF-TEST FAILED: {error}", file=sys.stderr)
        return 1
    finally:
        service.cleanup()
