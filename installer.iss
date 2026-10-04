[Setup]
AppId={{D741139A-1B51-4F87-8472-9C8429C2EAD1}
AppName=本地发票识别工具
AppVersion=1.0.0
AppPublisher=本地部署
DefaultDirName={autopf}\本地发票识别工具
DefaultGroupName=本地发票识别工具
DisableProgramGroupPage=yes
OutputDir=deliverables
OutputBaseFilename=本地发票识别工具-安装包
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
UninstallDisplayName=本地发票识别工具

[Languages]
Name: "chinesesimp"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Files]
Source: "dist\本地发票识别工具\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "THIRD_PARTY_NOTICES.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\本地发票识别工具"; Filename: "{app}\本地发票识别工具.exe"
Name: "{autodesktop}\本地发票识别工具"; Filename: "{app}\本地发票识别工具.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加图标："

[Run]
Filename: "{app}\本地发票识别工具.exe"; Description: "启动本地发票识别工具"; Flags: nowait postinstall skipifsilent
