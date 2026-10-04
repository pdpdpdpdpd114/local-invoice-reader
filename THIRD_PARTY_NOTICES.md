# 第三方组件说明

本工具使用第三方开源组件。便携包的 `licenses/` 目录保留了构建环境中可找到的完整许可证、版权和第三方说明副本；`licenses/inventory.json` 记录组件版本、包元数据中的许可证标识和复制的文件。该清单也可能包含测试或打包工具，不表示每个组件都作为运行代码进入程序。

- Python（PSF 许可证及随附说明）
- PySide6 / Qt for Python、shiboken6（包元数据标记 LGPL-3.0-only、GPL-2.0-only 或 GPL-3.0-only，具体组件以随附许可为准）
- RapidOCR（Apache-2.0）及其随包 OCR 模型；模型相关上游 PaddleOCR 的许可证也附在 `licenses/upstream/PaddleOCR/`
- ONNX Runtime（MIT）
- OpenCV、NumPy、Pillow 及其内含组件（详见随附许可证和第三方说明）
- pdfplumber（MIT）
- pypdfium2 / PDFium（包装层及内含组件的许可证详见其 `LICENSES` 和 `BUILD_LICENSES` 文本）
- openpyxl（MIT）
- PyInstaller（GPL-2.0-or-later，含其运行时例外条款）

部分 wheel 没有附完整开源许可证，因此仓库的 `third_party_licenses/` 另外保存从官方上游取得的文本。来源、版本和获取日期见该目录的 `SOURCES.md`；构建会将整个目录复制到 `licenses/upstream/`，不需要联网获取它们。

打包采用目录形式，Qt 和其他动态库保留在 `_internal/` 中。上述文本属于对应第三方组件，并不为本项目源码设定许可证。分发时请保留 `licenses/` 和本说明。
