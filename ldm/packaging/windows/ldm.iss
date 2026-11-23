; LDM's Windows installer (Inno Setup 6). build.py runs:
;   ISCC /DVersion=<version> /DSource=<the PyInstaller bundle> /DIcon=<ldm.ico> /O<output folder> ldm.iss
; Per user, into %LOCALAPPDATA%\Programs\LDM: no administrator rights. After copying the files it registers
; the browser bridge, magnet links, .torrent files and starting at login (`ldm install-browser-integration`),
; and puts LDM's folder on the user's PATH (so `ldm` works in a terminal); the uninstaller stops LDM and
; removes those. Downloads, settings and the download list stay.

#ifndef Version
  #error Version is required (build.py passes it)
#endif

[Setup]
AppId={{019D317B-3D07-4C2A-ACDC-292E3051C70C}
AppName=LDM
AppVersion={#Version}
AppVerName=LDM {#Version}
AppPublisher=LDM
AppPublisherURL=https://github.com/adiashishz/LDM
AppSupportURL=https://github.com/adiashishz/LDM/issues
AppUpdatesURL=https://github.com/adiashishz/LDM/releases
DefaultDirName={autopf}\LDM
DisableDirPage=yes
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputBaseFilename=LDM-Setup-{#Version}
SetupIconFile={#Icon}
UninstallDisplayIcon={app}\ldm-gui.exe
UninstallDisplayName=LDM
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; LDM is asked to quit first (PrepareToInstall); whatever still holds a file is closed by Windows.
CloseApplications=force
RestartApplications=no
; The user's PATH changes: tell running programs (new terminals see `ldm`).
ChangesEnvironment=yes

[InstallDelete]
; An update replaces the whole bundle: nothing of the old version's libraries may stay behind.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
Source: "{#Source}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\LDM"; Filename: "{app}\ldm-gui.exe"; Comment: "Download files, videos and torrents"; AppUserModelID: "io.github.adiashishz.LDM"

[Run]
Filename: "{app}\ldm.exe"; Parameters: "install-browser-integration"; Flags: runhidden waituntilterminated; StatusMsg: "Setting up the browser bridge, magnet links and .torrent files..."
Filename: "{app}\ldm-gui.exe"; Description: "Start LDM"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\ldm.exe"; Parameters: "quit"; Flags: runhidden waituntilterminated; RunOnceId: "QuitLDM"
Filename: "{app}\ldm.exe"; Parameters: "install-browser-integration --uninstall"; Flags: runhidden waituntilterminated; RunOnceId: "RemoveIntegration"

[Code]
const
  EnvironmentKey = 'Environment';  // the user's environment variables (HKEY_CURRENT_USER)

// An update: stop the running LDM (it saves every download's progress) before its files are replaced.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  if FileExists(ExpandConstant('{app}\ldm.exe')) then
    Exec(ExpandConstant('{app}\ldm.exe'), 'quit', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

// LDM's folder at the end of the user's PATH, once.
procedure AddToPath(Folder: String);
var
  Path: String;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Path) then
    Path := '';
  if Pos(';' + Uppercase(Folder) + ';', ';' + Uppercase(Path) + ';') > 0 then
    exit;
  if (Path <> '') and (Copy(Path, Length(Path), 1) <> ';') then
    Path := Path + ';';
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Path + Folder);
end;

// ... and out of it again; the rest of the PATH stays as it is.
procedure RemoveFromPath(Folder: String);
var
  Path: String;
  At: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Path) then
    exit;
  Path := ';' + Path + ';';
  At := Pos(';' + Uppercase(Folder) + ';', Uppercase(Path));
  if At = 0 then
    exit;
  Delete(Path, At, Length(Folder) + 1);
  RegWriteExpandStringValue(HKEY_CURRENT_USER, EnvironmentKey, 'Path', Copy(Path, 2, Length(Path) - 2));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    AddToPath(ExpandConstant('{app}'));
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RemoveFromPath(ExpandConstant('{app}'));
end;
