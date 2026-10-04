from pathlib import Path
import sys


# Allow the source tree to be launched directly during local verification.
# PyInstaller supplies this path through InvoiceTool.spec in the packaged app.
SOURCE_ROOT = Path(__file__).resolve().parent / "src"
if SOURCE_ROOT.exists():
    sys.path.insert(0, str(SOURCE_ROOT))

from invoice_tool.main import main


if __name__ == "__main__":
    raise SystemExit(main())
