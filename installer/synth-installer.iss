; SyntH Windows installer — Inno Setup 6 script
;
; Build with:  iscc installer\synth-installer.iss
; Or run:      installer\build_installer.ps1
;
; Design rules for this installer (see one-click.md):
;   * One option, no choices. No component picker, no types, no directory page.
;   * No admin. Everything lands in %LOCALAPPDATA% and the user profile.
;   * No console windows. Every helper runs hidden and is launched through
;     pythonw.exe so nothing flashes on screen.
;   * Personal details (name, location, timezone, engines, API keys) are NOT
;     asked here. They belong to the WebUI setup page, which the finish page
;     opens for you.

#define AppName "Synthetic Heart"
#define AppShortName "SyntH"
#define AppPublisher "Synthetic Heart Project"
#define AppURL "https://github.com/XargonWan/Synthetic_Heart"

; The version is supplied by the caller:  iscc /DAppVersion=1.2.3 installer\synth-installer.iss
; installer\build_installer.ps1 derives it (git tag, GitVersion, or a fallback)
; and CI passes the GitVersion output, which is the single source of truth for
; releases (see GitVersion.yml). There is no version file in the repository.
#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

; Windows version resources must be numeric, so a pre-release tag
; ("1.2.3-feat.4") is trimmed to "1.2.3" for VersionInfoVersion only. AppVersion
; keeps the full string everywhere the user sees it.
#if Pos("-", AppVersion) > 0
  #define AppVersionNumeric Copy(AppVersion, 1, Pos("-", AppVersion) - 1)
#else
  #define AppVersionNumeric AppVersion
#endif

; The default persona (skins\Rei) always ships, because the avatar has to work
; out of the box. The example personas are about 80 MB of models between them,
; which is a lot to download for something most people never switch to, so they
; are opt-in at build time:
;   iscc /DWithExampleSkins=1 installer\synth-installer.iss
#ifndef WithExampleSkins
  #define ExampleSkinsExclude "skins\Zero\*,skins\Miku\*,skins\Riko\*,"
#else
  #define ExampleSkinsExclude ""
#endif

[Setup]
AppId={{8F3A4B2C-9D1E-4F7A-B5C6-2E8D0A3F1B9E}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
VersionInfoVersion={#AppVersionNumeric}

; User scope: no UAC prompt, and the app directory is writable by the app.
DefaultDirName={localappdata}\Programs\{#AppShortName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; No questions: no directory page, no program group page, no component page.
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableWelcomePage=no
AllowNoIcons=yes

DefaultGroupName={#AppShortName}
OutputDir=Output
OutputBaseFilename=SyntH-Setup-{#AppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupIconFile=synth.ico
WizardImageFile=wizard-large.bmp
WizardSmallImageFile=wizard-small.bmp
UninstallDisplayIcon={app}\installer\synth.ico
UninstallDisplayName={#AppName}
MinVersion=10.0.16299

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
WelcomeLabel1=Welcome to the {#AppName} setup
WelcomeLabel2=This installs {#AppName} for you and nothing else.%n%nIt sets up its own private PostgreSQL and Python environment inside your user folder, so it needs no administrator rights and changes nothing system-wide.%n%nWhen it finishes it opens a page in your browser where you tell it who you are and which AI service to use. Everything else is already done.%n%nClick Next to continue.
FinishedHeadingLabel=Setup is complete
FinishedLabel=Setup has finished installing {#AppName}.%n%nIt is starting now and your browser will open the setup page. If it does not, use the {#AppShortName} shortcut in the Start Menu.
ReadyLabel1={#AppName} is ready to install.
ReadyLabel2a=Click Install to begin.
SelectDirLabel3=Setup will install {#AppName} into the following folder.

[Tasks]
; Unchecked by default: the Start Menu entry is enough for most people.
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: unchecked

[Files]
; The application tree. Generated and personal things are deliberately excluded:
; data\ holds the database, attachments and your encrypted API keys, and .env
; holds your settings. Shipping either would leak one install into another.
; NOTE: the Excludes list must stay on ONE line. A trailing backslash inside a
; quoted string is not a line continuation to Inno, it is a literal character,
; and the compiler then fails with "The system cannot find the path specified".
Source: "..\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion createallsubdirs; Excludes: ".git\*,.github\*,.venv*,.venv\*,logs\*,data\*,__pycache__\*,**\__pycache__\*,*.pyc,*.pyo,*.pyd,.tools\*,.gitnexus\*,.claude\*,.clinerules*,.codex\*,.continue\*,.cursor\*,.gemini\*,.zed\*,.idea\*,.vscode\*,node_modules\*,frontend\node_modules\*,mcp_servers\*,*.egg-info\*,docs\res\*,docs\wiki\*,installer\Output\*,installer\vendor\*,installer\*.iss,installer\*.ps1,installer\wizard-*.bmp,installer\synth-256.png,{#ExampleSkinsExclude}skins\2B\*,skins\temp\*,backups\*,tmp*,dist\*,build\*,site\*,htmlcov\*,.mypy_cache\*,.pytest_cache\*,.tox\*,.ruff_cache\*,old\*,SyntH_main,REWRITE-TASK.mm,SOUL-REWRITE-TASK.md,*.bak,*.swp,*.orig,*.log,*.log.*,.env,.env-*,.env.local,VENICE_NO_RESPONSE_REPORT.md,one-click.md,res\synth_webui\static\audio\tts\*"

; pgvector for the PostgreSQL we provision. Built by CI, absent in a source
; checkout, hence skipifsourcedoesntexist: the app degrades to in-memory SOUL
; memory instead of refusing to install.
Source: "vendor\pgvector\*"; DestDir: "{app}\installer\vendor\pgvector"; \
  Flags: recursesubdirs ignoreversion skipifsourcedoesntexist

[Icons]
; pythonw.exe, not a .bat: no console window ever appears.
Name: "{group}\{#AppShortName}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
  Parameters: """{app}\scripts\start_synth.py"""; WorkingDir: "{app}"; \
  IconFilename: "{app}\installer\synth.ico"; Comment: "Start {#AppName}"
Name: "{group}\{#AppShortName} setup page"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
  Parameters: """{app}\scripts\start_synth.py"" --setup"; WorkingDir: "{app}"; \
  IconFilename: "{app}\installer\synth.ico"; Comment: "Open the setup page"
Name: "{group}\Uninstall {#AppShortName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppShortName}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
  Parameters: """{app}\scripts\start_synth.py"""; WorkingDir: "{app}"; \
  IconFilename: "{app}\installer\synth.ico"; Tasks: desktopicon

[Run]
; Runs last, from the finish page, after the database and environment exist.
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\scripts\start_synth.py"""; \
  WorkingDir: "{app}"; StatusMsg: "Starting {#AppName}..."; \
  Flags: postinstall nowait skipifsilent; \
  Description: "Start {#AppName} and open the setup page"

[UninstallRun]
; Stop the app first so it releases the database and the log files.
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: """{app}\scripts\start_synth.py"" --stop"; \
  Flags: runhidden waituntilterminated; RunOnceId: "StopSyntH"
; Then the private PostgreSQL cluster: Windows will not delete files a running
; process holds open, and a stale cluster on a stale port breaks the next install.
Filename: "{app}\.venv\Scripts\pythonw.exe"; \
  Parameters: """{app}\scripts\bootstrap.py"" --stop-cluster --pg-bin ""{app}\pgsql\bin"""; \
  Flags: runhidden waituntilterminated; RunOnceId: "StopSyntHDatabase"

[UninstallDelete]
; Only what the installer created or the app generated. data\ and .env are kept
; deliberately: reinstalling then resumes with the same persona, history and
; keys instead of starting over. Delete them by hand for a clean slate.
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\pgsql"
Type: filesandordirs; Name: "{app}\ffmpeg"
Type: filesandordirs; Name: "{app}\.pytest_cache"
Type: filesandordirs; Name: "{app}\.ruff_cache"
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\uv.lock"

[Code]
const
  PrereqsLog = 'synth_prereqs.log';
  BootstrapLog = 'synth_bootstrap.log';

function StepFailed(const Title, Detail, LogName: String): Boolean;
var
  LogPath: String;
begin
  { The temp folder below is the same one install_prereqs.ps1 (its env:TEMP)
    and bootstrap.py write their logs into, so the path named in this message is
    a real one. The user profile constant is the percent-USERPROFILE form: a
    bare lowercase name such as the obvious-looking "userprofile" is not a
    constant at all, and because ExpandConstant is only evaluated here it fails
    at runtime rather than at compile time. tests/test_installer_payload.py
    guards against exactly that. No braces inside this comment: Inno comments
    do not nest, so an inner one would end the comment early. }
  LogPath := ExpandConstant('{%TEMP}\') + LogName;
  MsgBox(Title + #13#10#13#10 + Detail + #13#10#13#10 +
    'The log is here:' + #13#10 + LogPath + #13#10#13#10 +
    'You can retry this step yourself from a terminal in:' + #13#10 + ExpandConstant('{app}'),
    mbError, MB_OK);
  Result := False;
end;

function RunHidden(const Exe, Params: String; var ResultCode: Integer): Boolean;
begin
  Result := Exec(Exe, Params, ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  UvExe: String;
  Powershell: String;
begin
  if CurStep <> ssPostInstall then
    exit;

  Powershell := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');

  { Step 1: uv, PostgreSQL and pgvector. }
  WizardForm.StatusLabel.Caption := 'Installing dependencies (uv, PostgreSQL)...';
  if not RunHidden(Powershell,
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\scripts\install_prereqs.ps1') +
      '" -InstallDir "' + ExpandConstant('{app}') + '" -Quiet',
      ResultCode) or (ResultCode <> 0) then
  begin
    StepFailed('Dependencies could not be installed.',
      'install_prereqs.ps1 exited with code ' + IntToStr(ResultCode) + '.', PrereqsLog);
    exit;
  end;

  { Step 2: database, .env, Python environment. }
  WizardForm.StatusLabel.Caption := 'Setting up the database and Python environment...';
  UvExe := ExpandConstant('{%USERPROFILE}\.local\bin\uv.exe');
  if not FileExists(UvExe) then
    UvExe := 'uv';

  if not RunHidden(UvExe,
      'run --no-project python "' + ExpandConstant('{app}\scripts\bootstrap.py') +
      '" --portable --pg-bin "' + ExpandConstant('{app}\pgsql\bin') +
      '" --no-browser --log-file "' + ExpandConstant('{%TEMP}\' + BootstrapLog) + '"',
      ResultCode) or (ResultCode <> 0) then
  begin
    StepFailed('The database or the Python environment could not be set up.',
      'bootstrap.py exited with code ' + IntToStr(ResultCode) + '.', BootstrapLog);
    exit;
  end;
end;
