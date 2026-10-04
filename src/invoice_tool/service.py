from __future__ import annotations

import atexit
import math
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
import pdfplumber
import pypdfium2 as pdfium
from rapidocr import RapidOCR

from .applications import parse_application_docx
from .models import InvoiceRecord, OcrToken, PurchaseApplication
from .parser import UnsupportedInvoiceError, parse_invoice_tokens

SUPPORTED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
SUPPORTED_APPLICATION_EXTENSIONS = {".docx"}


@dataclass(frozen=True, slots=True)
class FolderScanResult:
    """The supported purchase files found directly inside one user folder."""

    folder: Path
    application_files: list[Path] = field(default_factory=list)
    invoice_files: list[Path] = field(default_factory=list)
    error: str = ""

    @property
    def valid(self) -> bool:
        return not self.error and bool(self.application_files) and bool(self.invoice_files)


def scan_import_folder(source_folder: str | Path) -> FolderScanResult:
    """Find one application and its invoices without descending into children."""
    folder = Path(source_folder)
    if not folder.exists() or not folder.is_dir():
        return FolderScanResult(folder=folder, error="文件夹不存在或无法访问。")
    try:
        files = [path for path in folder.iterdir() if path.is_file() and not _is_ignored_import_file(path)]
    except OSError:
        return FolderScanResult(folder=folder, error="无法读取文件夹内容。")
    files.sort(key=lambda path: str(path.resolve()).casefold())
    applications = [path for path in files if path.suffix.lower() in SUPPORTED_APPLICATION_EXTENSIONS]
    invoices = [path for path in files if path.suffix.lower() in SUPPORTED_EXTENSIONS]
    if not applications:
        error = "未找到 DOCX 申请表。"
    elif not invoices:
        error = "未找到 PDF、JPG、JPEG 或 PNG 发票。"
    else:
        error = ""
    return FolderScanResult(folder=folder, application_files=applications, invoice_files=invoices, error=error)


def _is_ignored_import_file(path: Path) -> bool:
    """Skip common temporary/hidden files while leaving source records untouched."""
    if path.name.startswith((".", "~$")):
        return True
    try:
        attributes = getattr(path.stat(), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attributes & 0x2)  # FILE_ATTRIBUTE_HIDDEN on Windows


class InvoiceService:
    def __init__(self) -> None:
        self._workspace = Path(tempfile.mkdtemp(prefix="local-invoice-reader-"))
        self._ocr: RapidOCR | None = None
        atexit.register(self.cleanup)

    def cleanup(self) -> None:
        if self._workspace.exists():
            shutil.rmtree(self._workspace, ignore_errors=True)

    def parse_file(self, source_file: str | Path) -> InvoiceRecord:
        path = Path(source_file)
        if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise UnsupportedInvoiceError("仅支持 PDF、JPG、JPEG 和 PNG 文件。")
        if not path.exists():
            raise FileNotFoundError(path)
        if path.suffix.lower() == ".pdf":
            return self._parse_pdf(path)
        return self._parse_image(path)

    def parse_application(self, source_file: str | Path, existing_numbers: set[str] | None = None) -> PurchaseApplication:
        """Read a local DOCX application without invoking Office or a network service."""
        return parse_application_docx(source_file, existing_numbers)

    def _parse_pdf(self, path: Path) -> InvoiceRecord:
        preview = self._render_pdf(path, 150)
        try:
            with pdfplumber.open(path) as document:
                if not document.pages:
                    raise UnsupportedInvoiceError("PDF 没有页面。")
                page = document.pages[0]
                words = page.extract_words(x_tolerance=1, y_tolerance=2, keep_blank_chars=False)
                tokens = [
                    OcrToken(word["text"], word["x0"], word["top"], word["x1"], word["bottom"], 1.0)
                    for word in words
                    if word.get("text", "").strip()
                ]
                if len(tokens) >= 20:
                    record = parse_invoice_tokens(tokens, float(page.width), float(page.height), path, "PDF 文字层")
                    record.preview_path = preview
                    return record
        except UnsupportedInvoiceError:
            raise
        except Exception:
            # 文字层不可用时，安全回退到本地 OCR；不向外发送文件。
            pass

        scan = self._render_pdf(path, 300)
        return self._parse_image(scan, source_file=path, preview_path=preview, route="扫描 PDF 本地 OCR")

    def _parse_image(
        self,
        path: Path,
        source_file: Path | None = None,
        preview_path: Path | None = None,
        route: str = "图片本地 OCR",
    ) -> InvoiceRecord:
        image = self._read_image(path)
        prepared = self._preprocess(image)
        tokens = self._ocr_tokens(prepared)
        record = parse_invoice_tokens(tokens, float(prepared.shape[1]), float(prepared.shape[0]), source_file or path, route)
        record.preview_path = preview_path or path
        return record

    def _render_pdf(self, path: Path, dpi: int) -> Path:
        document = pdfium.PdfDocument(str(path))
        try:
            page = document[0]
            image = page.render(scale=dpi / 72).to_pil()
            output = self._workspace / f"{path.stem}-{dpi}.png"
            image.save(output)
            return output
        finally:
            document.close()

    @staticmethod
    def _read_image(path: Path) -> np.ndarray:
        raw = np.fromfile(str(path), dtype=np.uint8)
        image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
        if image is None:
            raise UnsupportedInvoiceError("无法读取图片。")
        return image

    def _ocr_tokens(self, image: np.ndarray) -> list[OcrToken]:
        if self._ocr is None:
            self._ocr = RapidOCR()
        result = self._ocr(image)
        tokens: list[OcrToken] = []
        for box, text, score in zip(result.boxes, result.txts, result.scores, strict=True):
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            tokens.append(OcrToken(text, min(xs), min(ys), max(xs), max(ys), float(score)))
        return tokens

    def _preprocess(self, image: np.ndarray) -> np.ndarray:
        image = self._perspective_correct(image)
        image = self._deskew(image)
        if image.shape[1] < 1600:
            ratio = 1600 / image.shape[1]
            image = cv2.resize(image, None, fx=ratio, fy=ratio, interpolation=cv2.INTER_CUBIC)
        lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
        l_channel, a_channel, b_channel = cv2.split(lab)
        l_channel = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l_channel)
        return cv2.cvtColor(cv2.merge((l_channel, a_channel, b_channel)), cv2.COLOR_LAB2BGR)

    @staticmethod
    def _perspective_correct(image: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 45, 135)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        image_area = image.shape[0] * image.shape[1]
        for contour in sorted(contours, key=cv2.contourArea, reverse=True):
            if cv2.contourArea(contour) < image_area * 0.55:
                break
            perimeter = cv2.arcLength(contour, True)
            polygon = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
            if len(polygon) != 4:
                continue
            points = polygon.reshape(4, 2).astype("float32")
            ordered = _order_points(points)
            width = int(max(np.linalg.norm(ordered[1] - ordered[0]), np.linalg.norm(ordered[2] - ordered[3])))
            height = int(max(np.linalg.norm(ordered[3] - ordered[0]), np.linalg.norm(ordered[2] - ordered[1])))
            # A clean electronic-invoice screenshot contains a large red
            # inner table.  It is not the document edge: using it would cut
            # away the title and invoice number above the table.
            if width < image.shape[1] * 0.88 or height < image.shape[0] * 0.88:
                continue
            if width < 100 or height < 100:
                continue
            destination = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], dtype="float32")
            return cv2.warpPerspective(image, cv2.getPerspectiveTransform(ordered, destination), (width, height))
        return image

    @staticmethod
    def _deskew(image: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        lines = cv2.HoughLinesP(cv2.Canny(gray, 50, 150), 1, np.pi / 180, 120, minLineLength=image.shape[1] * 0.25, maxLineGap=20)
        if lines is None:
            return image
        angles = []
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            angle = math.degrees(math.atan2(y2 - y1, x2 - x1))
            if abs(angle) <= 8:
                angles.append(angle)
        if not angles:
            return image
        angle = float(np.median(angles))
        if abs(angle) < 0.4:
            return image
        matrix = cv2.getRotationMatrix2D((image.shape[1] / 2, image.shape[0] / 2), angle, 1.0)
        return cv2.warpAffine(image, matrix, (image.shape[1], image.shape[0]), borderMode=cv2.BORDER_REPLICATE)


def _order_points(points: np.ndarray) -> np.ndarray:
    ordered = np.zeros((4, 2), dtype="float32")
    summed = points.sum(axis=1)
    ordered[0] = points[np.argmin(summed)]
    ordered[2] = points[np.argmax(summed)]
    differences = np.diff(points, axis=1).reshape(-1)
    ordered[1] = points[np.argmin(differences)]
    ordered[3] = points[np.argmax(differences)]
    return ordered
