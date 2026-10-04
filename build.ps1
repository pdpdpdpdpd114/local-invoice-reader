param(
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'

function Assert-WorkspacePath([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path)
    $prefix = $projectRoot.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw "构建目标不在项目目录内: $resolved"
    }
}

function Assert-NativeSuccess([string]$Step) {
    if ($LASTEXITCODE -ne 0) {
        throw "$Step 失败，退出码 $LASTEXITCODE。"
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    throw '未找到 .venv。请先按 README 创建并安装依赖。'
}

Push-Location $projectRoot
try {
    if (-not $SkipTests) {
        $testBase = Join-Path $projectRoot 'tmp\pytest-build'
        Assert-WorkspacePath $testBase
        & $python -m pytest -q --basetemp $testBase
        Assert-NativeSuccess '自动化测试'
    }

    $bundle = Join-Path $projectRoot 'dist\本地发票识别工具'
    Assert-WorkspacePath $bundle
    Assert-WorkspacePath (Join-Path $projectRoot 'build')
    & $python -m PyInstaller --clean --noconfirm InvoiceTool.spec
    Assert-NativeSuccess '程序打包'
    if (-not (Test-Path -LiteralPath (Join-Path $bundle '本地发票识别工具.exe'))) {
        throw '打包没有生成主程序。'
    }

    Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md'), (Join-Path $projectRoot 'THIRD_PARTY_NOTICES.md') -Destination $bundle -Force
    # Copy only installed dependency notices and checked-in upstream licenses.
    # No user documents, output folders or private samples are read here.
    @'
import importlib.metadata as metadata
import json
from pathlib import Path
import re
import shutil
import sys

project = Path(sys.argv[1]).resolve()
bundle = Path(sys.argv[2]).resolve()
if not bundle.is_relative_to(project):
    raise ValueError("License destination is outside the project")
destination = bundle / "licenses"
destination.mkdir(parents=True, exist_ok=True)
inventory = []
text_extensions = {"", ".txt", ".md", ".rst", ".html", ".ijg", ".json", ".rtf"}
for distribution in sorted(metadata.distributions(), key=lambda d: d.metadata.get("Name", "").lower()):
    name = distribution.metadata.get("Name", "unknown")
    if name == "local-invoice-reader":
        continue
    component = re.sub(r"[^A-Za-z0-9_.-]", "_", f"{name}-{distribution.version}")
    copied = []
    for entry in distribution.files or []:
        relative = Path(str(entry))
        if relative.is_absolute() or ".." in relative.parts:
            continue
        if relative.suffix.lower() not in text_extensions:
            continue
        if not re.search(r"license|licence|copying|notice|copyright", str(relative), re.IGNORECASE):
            continue
        source = Path(distribution.locate_file(entry))
        if not source.is_file():
            continue
        target = destination / component / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        copied.append(target.relative_to(destination).as_posix())
    inventory.append({
        "name": name,
        "version": distribution.version,
        "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License"),
        "project_urls": distribution.metadata.get_all("Project-URL", []),
        "license_files": copied,
    })

supplements = project / "third_party_licenses"
if not supplements.is_dir():
    raise FileNotFoundError("Missing third_party_licenses supplements")
shutil.copytree(supplements, destination / "upstream", dirs_exist_ok=True)
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
    shutil.copy2(python_license, destination / "Python-LICENSE.txt")
(destination / "inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"Collected dependency license files: {sum(len(item['license_files']) for item in inventory)}")
'@ | & $python - $projectRoot $bundle
    Assert-NativeSuccess '第三方许可证收集'

    $delivery = Join-Path $projectRoot 'deliverables'
    Assert-WorkspacePath $delivery
    New-Item -ItemType Directory -Force -Path $delivery | Out-Null
    $zip = Join-Path $delivery '本地发票识别工具-portable.zip'
    $pendingZip = Join-Path $delivery '本地发票识别工具-portable.new.zip'
    Assert-WorkspacePath $zip
    Assert-WorkspacePath $pendingZip
    Compress-Archive -Path (Join-Path $bundle '*') -DestinationPath $pendingZip -Force
    Move-Item -LiteralPath $pendingZip -Destination $zip -Force
    Write-Output "已生成便携包: $zip"

    $iscc = Join-Path $projectRoot '.tools\InnoSetup\ISCC.exe'
    if (Test-Path -LiteralPath $iscc) {
        & $iscc (Join-Path $projectRoot 'installer.iss')
        Assert-NativeSuccess '安装包编译'
        Write-Output "已生成安装包: $(Join-Path $delivery '本地发票识别工具-安装包.exe')"
    }
    else {
        Write-Warning '未检测到本地 Inno Setup 编译器，已生成便携包。安装 Inno Setup 后再次运行本脚本可生成安装包。'
    }
}
finally {
    Pop-Location
}
