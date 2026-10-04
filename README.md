# FSEC 本地发票与采购识别工具

这是一个面向 FSEC 车队、完全离线运行的 Windows 桌面工具。它可一次读取一份或多份 DOCX 经费使用申请表和多张电子普通发票，识别申请人、部门、采购时间、采购金额与发票商品明细，并在人工确认后导出采购记录 Excel。

## 隐私与边界

- 不调用大模型、云端 OCR、外部 API 或登录服务。
- 图片识别使用安装包内的 RapidOCR/ONNX 本地模型；断网可用。
- 原始发票默认只读，不移动、不删除；只有用户在“批量重命名”预览中明确确认后，才会修改文件名。
- 第一版仅支持电子普通发票的标准商品明细版式；收据、专票、卷票、行程单、手写票据会明确提示不支持。

## 日常使用

### 下载与启动

在本仓库的 [Releases](https://github.com/pdpdpdpdpd114/local-invoice-reader/releases) 页面选择最新版本，在 **Assets** 中下载 `本地发票识别工具-portable.zip`。这是 Windows 10/11 x64 便携包，不需要安装 Python。完整解压到一个可写文件夹后，启动其中的 `本地发票识别工具.exe`；请保留同目录下的 `_internal` 和 `licenses` 文件夹。GitHub 的 **Code → Download ZIP** 提供源码，不能直接当作便携包使用。

1. 启动 `本地发票识别工具.exe`。
2. 点击顶部“导入申请表和发票”，在同一个导入窗口中可选择单笔资料（1 份 DOCX 申请表和 1 至多张 PDF/JPG/PNG 发票），或按住 Ctrl / Shift 选择多个资料文件夹。原始文件始终留在原位置。
3. 多文件夹导入时，程序只扫描每个文件夹的当前层，可识别 1 至多份 DOCX 申请表和全部 PDF/JPG/PNG 发票；每份申请表会成为独立申请单，也可直接将多个文件夹拖入程序。
4. 同一文件夹有多份申请表时，程序按申请表金额与发票价税合计自动归属。只有唯一的金额组合会自动分配；其他发票保留在“待分配发票”，可右键选择目标申请单。未分配发票不能确认或导出。
5. 文件夹内缺申请表、缺发票或申请表无法读取时会跳过相关资料并显示原因；可读取但字段不完整的申请表仍会导入，供人工补充。子文件夹、隐藏文件、Word 临时文件和其他格式不会导入。
6. 程序从申请表中读取部门、申请人、采购时间与金额，并自动生成申请单编号。例如电控组在 2026 年 7 月 29 日为 `DK20260729`；同日同组后续申请单依次加 `-02`、`-03`。
7. 在左侧选择申请单，复核并确认申请人、日期、金额、付款日期和申请单编号；可点击“打开原申请表”查看本地原件。
8. 逐张选择发票复核商品明细并确认。申请表下可继续点击“导入发票”追加票据。
9. 申请单检查器会显示“发票总价 - 采购单金额”。差额超过 0.01 元时，须勾选“已人工确认金额差异”后才能导出。
10. 所有申请表和发票确认后，点击“导出 Excel”，生成 13 列采购记录表：申请单编号、申请人、申请日期、商品明细、金额、税额、价税合计、发票号码和付款日期。
11. 点击顶部“批量重命名”可预览并整理已确认发票。文件名为“组别首字母-发票号码.原扩展名”，例如 `XJ-00000000000000000001.pdf`（示例编号）；未确认、待分配、冲突或字段不完整的文件会显示原因并保持原名。完成后可在本次程序运行期间点击“撤销上次重命名”恢复原文件名。

原“新建日期分组 + 导入发票”流程仍保留作手工兜底；使用该流程时，导出前需要在申请单检查器中补齐申请人、申请单编号和采购单金额。

## 快捷键

- `Ctrl+N`：新建文件夹
- `Ctrl+I`：导入申请表和发票
- `Ctrl+O`：向选中文件夹导入发票
- `Ctrl+E`：修改选中文件夹
- `Ctrl+Enter`：确认当前发票
- `Ctrl+Shift+Enter`：取消当前发票确认
- `Ctrl+S`：导出 Excel

每个商品占一行。导出的 Excel 使用 13 列采购记录格式；申请单编号、申请人、申请日期和付款日期在同一申请单的商品行中纵向合并，价税合计和发票号码在同一张发票的商品行中纵向合并。编号按文本保存，避免长发票号码被改写为科学计数法。

## 开发与构建

已验证的构建环境为 Windows x64、Python 3.12。首次配置需要联网安装依赖；安装完成后的识别和打包均使用本地文件。在项目根目录打开 PowerShell，执行：

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e .
.\.venv\Scripts\python.exe run_app.py
```

`requirements.lock` 固定了本次发布使用的运行、测试和打包依赖。测试与构建命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\build.ps1
```

构建脚本默认先运行测试，测试或打包失败时立即退出。成功后生成 `deliverables\本地发票识别工具-portable.zip`，包含程序、本地 OCR 模型、README、第三方说明和许可证副本。已经完成测试时，可使用 `.\build.ps1 -SkipTests`。如果 `.tools\InnoSetup\ISCC.exe` 已存在，还会生成安装包；没有该编译器时只生成便携包，不自动安装或下载它。发布下载包不自动下载模型，也不包含自动更新或网络代码。

### 本地样张验收

仓库只保留合成数据测试，不包含真实申请表或发票。三个真实样张测试需要同时提供样张路径和 JSON 格式的期望值；缺少任一配置时会跳过：

| 样张路径变量 | 期望值变量 |
| --- | --- |
| `INVOICE_PDF_SAMPLE` | `INVOICE_PDF_EXPECTED` |
| `INVOICE_IMAGE_SAMPLE` | `INVOICE_IMAGE_EXPECTED` |
| `INVOICE_IMAGE_INNER_TABLE_SAMPLE` | `INVOICE_IMAGE_INNER_TABLE_EXPECTED` |

期望 JSON 必须包含 `invoice_number`、`total_with_tax` 和按票面顺序排列的 `items`。每个商品至少包含 `amount`；可补充 `project_name`、`specification`、`unit`、`quantity`、`unit_price`、`tax_rate`、`tax_amount`，测试会逐项核对。下列数据仅演示配置格式，应替换为自己的本地样张和期望值：

```powershell
$env:INVOICE_PDF_SAMPLE = 'C:\private-samples\sample.pdf'
$env:INVOICE_PDF_EXPECTED = '{"invoice_number":"00000000000000000001","total_with_tax":"101.00","items":[{"amount":"100.00","tax_amount":"1.00","tax_rate":"0.01"}]}'
.\.venv\Scripts\python.exe -m pytest tests/test_samples.py -q
```

打包后的程序也支持 `本地发票识别工具.exe --self-test 本地样张路径`，可在不打开主界面的情况下验证本地解析和金额检查。样张、期望配置、验收输出和截图应保留在本地；`tmp/`、`output/`、`deliverables/` 均不提交到仓库。

## 核对规则

- 数量 × 单价（四舍五入至 0.01）= 金额
- 金额 × 税率（四舍五入至 0.01）= 税额
- 商品金额合计、商品税额合计、价税合计与票面合计相符
- 同一批次内不允许重复发票号码
- 同一批次内不允许重复申请单编号
- 发票价税合计之和与申请表金额的差额不超过 0.01 元时自动通过；其余情况需要人工确认差异

规格型号可以为空；其他明细字段和发票号码、价税合计必须确认。
