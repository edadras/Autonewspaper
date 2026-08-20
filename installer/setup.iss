; Inno Setup script for AI Newspaper Studio.
;
; Build the PyInstaller bundle first:
;     pyinstaller installer/ai_newspaper_studio.spec --noconfirm
; then compile this file with Inno Setup 6:
;     iscc installer\setup.iss
;
; The result is installer\output\AI-Newspaper-Studio-Setup.exe

#define AppName "AI Newspaper Studio"
#define AppVersion "1.0.0"
#define AppPublisher "AI Newspaper Studio"
#define AppExeName "AINewspaperStudio.exe"

[Setup]
AppId={{7C4B1F42-9C1E-4D5B-9E6B-3A1C0D8F2E11}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\AI Newspaper Studio
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=output
OutputBaseFilename=AI-Newspaper-Studio-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile=..\LICENSE.txt
SetupIconFile=..\resources\app.ico
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "persian"; MessagesFile: "compiler:Languages\Persian.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\AINewspaperStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\README.md"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\*"; DestDir: "{app}\docs"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\User guide"; Filename: "{app}\docs\user-guide.md"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: quicklaunchicon

[Dirs]
Name: "{localappdata}\AINewspaperStudio"; Flags: uninsneveruninstall
Name: "{localappdata}\AINewspaperStudio\projects"; Flags: uninsneveruninstall
Name: "{localappdata}\AINewspaperStudio\templates"; Flags: uninsneveruninstall
Name: "{localappdata}\AINewspaperStudio\logs"; Flags: uninsneveruninstall

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExeName}"; Parameters: "--diagnostics"; Description: "Run system diagnostics"; Flags: postinstall skipifsilent unchecked runascurrentuser

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Messages]
persian.WelcomeLabel2=این برنامه استودیو روزنامه هوشمند را روی رایانه شما نصب می‌کند.%n%nپیش از ادامه، بستن سایر برنامه‌ها توصیه می‌شود.

[Code]
function InitializeSetup(): Boolean;
begin
  Result := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  { The application creates its own data directory on first start; the
    [Dirs] section only makes sure it survives an uninstall. }
  if CurStep = ssPostInstall then
  begin
    Log('AI Newspaper Studio installed to ' + ExpandConstant('{app}'));
  end;
end;
