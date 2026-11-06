; SPDX-FileCopyrightText: 2026 The Nature AI Worker Authors
; SPDX-License-Identifier: Apache-2.0
;
; Per-user Windows installer for Nature AI Worker (Standalone). Compiled with Inno Setup 6
; (packaging/windows/innosetup.json) from a folder staged by scripts/windows_installer.py.
;
; The installer carries no install logic of its own. Before anything is written it unpacks
; the pinned Python runtime (SHA-256 checked) and runs bootstrap.py, which hands over to the
; release's own upgrader: `install` for a new installation, `upgrade` (verified, transactional,
; rolled back on failure) for an existing one. Only when that succeeds does Setup add the
; Start-menu entries, the licenses and the Apps & Features entry.
;
; Standalone and the Platform Client (S-C) are separate installations (own user, folders, port):
; Setup never looks at Platform Client folders. The release's upgrader refuses Standalone code, data
; or workspace inside them, and a data folder marked as Platform Client data (no in-place switch).
;
; Silent use (tests, managed deployment):
;   Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /LOG=setup.log [/DIR=...] [/DATADIR=...] [/WORKSPACE=...]
;   An existing installation is only upgraded with /CONFIRMUPGRADE=<new version>.

#include "defines.iss"
#define AppName "Nature AI Worker"

[Setup]
AppId={{6F3C2B1E-8D4A-4E7B-9C52-1A0E7D3F5B94}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=The Nature AI Worker Authors
AppPublisherURL=https://github.com/nature-ai-corp/nature-ai-worker
VersionInfoVersion={#AppVersionNumeric}
DefaultDirName={autopf}\NatureAIWorker
DefaultGroupName={#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
UsePreviousAppDir=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; tar.exe ships with Windows 10 1803 (build 17134) and later.
MinVersion=10.0.17134
LicenseFile=licenses\LICENSE.txt
OutputDir=output
OutputBaseFilename=NatureAIWorker-Setup-{#FileLabel}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
SetupMutex=NatureAIWorkerStandaloneSetup
CloseApplications=no
UninstallDisplayName={#AppName}
UninstallDisplayIcon={sys}\shell32.dll,13

[Files]
Source: "payload.tar"; Flags: dontcopy
Source: "licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\bin\nature-worker-desktop.cmd"; WorkingDir: "{app}"; \
  Comment: "Start Nature AI Worker and open it in your browser"
Name: "{group}\Remove Nature AI Worker data"; Filename: "{cmd}"; \
  Parameters: "/K """"{app}\bin\nature-worker-upgrade.cmd"" remove-data"""; WorkingDir: "{app}"; \
  Comment: "Permanently delete settings, saved API keys and history (asks first)"
Name: "{group}\Licenses"; Filename: "{app}\licenses"

[UninstallDelete]
; Only what the installation created. Data and workspace live elsewhere and are kept.
Type: filesandordirs; Name: "{app}\versions"
Type: filesandordirs; Name: "{app}\bin"
Type: filesandordirs; Name: "{app}\upgrade"
Type: files; Name: "{app}\current"
Type: files; Name: "{app}\installation.json"
Type: files; Name: "{app}\trusted-keys.json"
Type: dirifempty; Name: "{app}"

[Code]
const
  SettingsKey = 'Software\NatureAIWorker\Standalone';

var
  DataDir, Workspace, Installed: String;
  Upgrading: Boolean;

{ Release numbers of "X.Y.Z" or "X.Y.Z+label"; a label never makes a build newer. }
function VersionPart(V: String; Index: Integer): Integer;
var
  I, N: Integer;
begin
  I := Pos('+', V);
  if I > 0 then V := Copy(V, 1, I - 1);
  for N := 0 to Index - 1 do
  begin
    I := Pos('.', V);
    if I = 0 then begin Result := -1; exit; end;
    V := Copy(V, I + 1, Length(V));
  end;
  I := Pos('.', V);
  if I > 0 then V := Copy(V, 1, I - 1);
  Result := StrToIntDef(V, -1);
end;

function CompareVersions(A, B: String): Integer;
var
  N: Integer;
begin
  Result := 0;
  for N := 0 to 2 do
    if Result = 0 then
      Result := VersionPart(A, N) - VersionPart(B, N);
end;

function Inside(Path, Folder: String): Boolean;
begin
  Path := AddBackslash(Lowercase(ExpandFileName(Path)));
  Folder := AddBackslash(Lowercase(ExpandFileName(Folder)));
  Result := Copy(Path, 1, Length(Folder)) = Folder;
end;

function FolderIsEmpty(Folder: String): Boolean;
var
  F: TFindRec;
begin
  Result := True;
  if FindFirst(AddBackslash(Folder) + '*', F) then
  begin
    try
      repeat
        if (F.Name <> '.') and (F.Name <> '..') then Result := False;
      until (not Result) or (not FindNext(F));
    finally
      FindClose(F);
    end;
  end;
end;

procedure InitializeWizard();
begin
  DataDir := ExpandConstant('{param:DATADIR|{localappdata}\NatureAIWorker\data}');
  Workspace := ExpandConstant('{param:WORKSPACE|{userdocs}\Nature AI Worker}');
end;

function ReadInstalled(): String;
var
  S: AnsiString;
begin
  Result := '';
  if LoadStringFromFile(ExpandConstant('{app}\current'), S) then Result := Trim(String(S));
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo,
  MemoComponentsInfo, MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Installed := ReadInstalled();
  if Installed <> '' then
    Result := 'Upgrade Nature AI Worker ' + Installed + ' to {#AppVersion}.' + NewLine + NewLine +
      'Running work is allowed to finish, your data is backed up and migrated, and the new version ' +
      'is checked before it is used. If any step fails, ' + Installed + ' and its data are restored.' +
      NewLine + NewLine + 'Program folder:' + NewLine + Space + ExpandConstant('{app}')
  else
    Result := 'Install Nature AI Worker {#AppVersion}.' + NewLine + NewLine +
      'Program folder:' + NewLine + Space + ExpandConstant('{app}') + NewLine +
      'Data (settings, saved API keys, history):' + NewLine + Space + DataDir + NewLine +
      'Workspace (the files the agent may use):' + NewLine + Space + Workspace + NewLine + NewLine +
      'Existing data in that folder is kept and migrated, after a backup.';
end;

function LogTail(LogFile: String): String;
var
  Lines: TArrayOfString;
  I, First: Integer;
begin
  Result := '';
  if LoadStringsFromFile(LogFile, Lines) then
  begin
    First := GetArrayLength(Lines) - 8;
    if First < 0 then First := 0;
    for I := First to GetArrayLength(Lines) - 1 do
      Result := Result + Lines[I] + #13#10;
  end;
end;

function RunStep(Exe, Params: String; var Code: Integer): Boolean;
begin
  Log('Running: ' + Exe + ' ' + Params);
  Result := Exec(Exe, Params, ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, Code) and (Code = 0);
  Log('Exit code: ' + IntToStr(Code));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  App, Tar, Payload, Python, LogFile, Params: String;
  Code: Integer;
begin
  Result := '';
  App := ExpandConstant('{app}');
  Installed := ReadInstalled();
  Upgrading := Installed <> '';
  LogFile := AddBackslash(GetTempDir()) + 'NatureAIWorker-Setup-{#FileLabel}-' +
    GetDateTimeString('yyyymmdd-hhnnss', #0, #0) + '.log';

  { Never overwrite an installation silently, never downgrade, never reuse a foreign folder. }
  if Upgrading then
  begin
    if CompareVersions(Installed, '{#AppVersion}') = 0 then
    begin
      Result := 'Nature AI Worker ' + Installed + ' is already installed. Nothing was changed.';
      exit;
    end;
    if CompareVersions(Installed, '{#AppVersion}') > 0 then
    begin
      Result := 'A newer version (' + Installed + ') is installed. Going back to an older version ' +
        'is not done by Setup; use "nature-worker-upgrade rollback". Nothing was changed.';
      exit;
    end;
    if WizardSilent() and (ExpandConstant('{param:CONFIRMUPGRADE|}') <> '{#AppVersion}') then
    begin
      Result := 'Nature AI Worker ' + Installed + ' is installed. A silent upgrade needs ' +
        '/CONFIRMUPGRADE={#AppVersion}. Nothing was changed.';
      exit;
    end;
  end
  else if FileExists(AddBackslash(App) + 'installation.json') then
  begin
    Result := 'The installation in ' + App + ' has no active version (an interrupted upgrade?). Run ' +
      '"nature-worker-upgrade recover" there first. Nothing was changed.';
    exit;
  end
  else if DirExists(App) and not FolderIsEmpty(App) then
  begin
    Result := App + ' already exists and is not empty. Nothing was changed.';
    exit;
  end
  else if FileExists(ExpandConstant('{localappdata}\NatureAIWorker\installation.json')) then
  begin
    Result := 'Nature AI Worker is already installed in ' + ExpandConstant('{localappdata}\NatureAIWorker') +
      ' (installed with nature-worker-upgrade). Upgrade it with nature-worker-upgrade. Nothing was changed.';
    exit;
  end
  else if Inside(DataDir, App) or Inside(Workspace, App) then
  begin
    Result := 'The data and workspace folders must not be inside the program folder. Nothing was changed.';
    exit;
  end;

  { The payload: the signed release bundle (with the pinned runtime) and bootstrap.py. }
  ExtractTemporaryFile('payload.tar');
  Payload := ExpandConstant('{tmp}\payload.tar');
  if Lowercase(GetSHA256OfFile(Payload)) <> '{#PayloadSha256}' then
  begin
    Result := 'The installer is damaged (payload checksum mismatch). Download it again.';
    exit;
  end;
  Tar := ExpandConstant('{sys}\tar.exe');
  if not RunStep(Tar, '-xf "' + Payload + '" -C "' + ExpandConstant('{tmp}') + '"', Code) then
  begin
    Result := 'Could not unpack the installer payload (tar.exe exit ' + IntToStr(Code) + ').';
    exit;
  end;
  if Lowercase(GetSHA256OfFile(ExpandConstant('{tmp}\payload\bundle\{#RuntimePath}'))) <> '{#RuntimeSha256}' then
  begin
    Result := 'The bundled Python runtime does not match its pinned SHA-256. Nothing was installed.';
    exit;
  end;
  CreateDir(ExpandConstant('{tmp}\boot'));
  if not RunStep(Tar, '-xzf "' + ExpandConstant('{tmp}\payload\bundle\{#RuntimePath}') + '" -C "' +
    ExpandConstant('{tmp}\boot') + '"', Code) then
  begin
    Result := 'Could not unpack the bundled Python runtime (tar.exe exit ' + IntToStr(Code) + ').';
    exit;
  end;

  Python := ExpandConstant('{tmp}\boot\python\python.exe');
  Params := '-I "' + ExpandConstant('{tmp}\payload\bootstrap.py') + '" --bundle "' +
    ExpandConstant('{tmp}\payload\bundle') + '" --prefix "' + App + '" --confirm-version "{#AppVersion}"' +
    ' --log "' + LogFile + '"';
  if not Upgrading then
  begin
    Params := Params + ' --data-dir "' + DataDir + '" --workspace "' + Workspace + '"';
#if TrustKey
    Params := Params + ' --trust-key "' + ExpandConstant('{tmp}\payload\trusted-dev-key.json') + '"';
#endif
  end;
  WizardForm.PreparingLabel.Caption := 'Installing Nature AI Worker {#AppVersion}. This takes a minute or two...';
  if not RunStep(Python, Params, Code) then
  begin
    if Upgrading then
      { No line here may start with '#': the preprocessor would read it as a directive. }
      Result := 'The upgrade to {#AppVersion} did not complete (exit ' + IntToStr(Code) + '). Nature AI Worker ' +
        Installed + ' remains installed, or has been restored. Details:' + #13#10 + LogTail(LogFile) + #13#10 +
        'Full log: ' + LogFile
    else
      Result := 'Nature AI Worker was not installed (exit ' + IntToStr(Code) + '). Details:' + #13#10 +
        LogTail(LogFile) + #13#10 + 'Full log: ' + LogFile;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and not Upgrading then
  begin
    RegWriteStringValue(HKCU, SettingsKey, 'DataDir', DataDir);
    RegWriteStringValue(HKCU, SettingsKey, 'Workspace', Workspace);
  end;
end;

function InitializeUninstall(): Boolean;
var
  Cmd, Output: String;
  Text: AnsiString;
  Code: Integer;
begin
  Result := True;
  Cmd := ExpandConstant('{app}\bin\nature-worker-upgrade.cmd');
  if not FileExists(Cmd) then exit;
  Output := AddBackslash(GetTempDir()) + 'nature-worker-uninstall-check.txt';
  Exec(ExpandConstant('{cmd}'), '/C ""' + Cmd + '" uninstall-check > "' + Output + '" 2>&1"', '',
    SW_HIDE, ewWaitUntilTerminated, Code);
  if Code <> 0 then
  begin
    LoadStringFromFile(Output, Text);
    SuppressibleMsgBox('Nature AI Worker cannot be removed right now:' + #13#10#13#10 + String(Text),
      mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Data, Space: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RegQueryStringValue(HKCU, SettingsKey, 'DataDir', Data);
    RegQueryStringValue(HKCU, SettingsKey, 'Workspace', Space);
    RegDeleteKeyIncludingSubkeys(HKCU, SettingsKey);
    RegDeleteKeyIfEmpty(HKCU, 'Software\NatureAIWorker');
    if not UninstallSilent() then
      MsgBox('Nature AI Worker was removed. Your data and files were kept:' + #13#10#13#10 +
        'Data: ' + Data + #13#10 + 'Workspace: ' + Space + #13#10#13#10 +
        'Installing Nature AI Worker again uses them. To delete the data, use "Remove Nature AI Worker ' +
        'data" before uninstalling, or delete the folder yourself.', mbInformation, MB_OK);
  end;
end;
