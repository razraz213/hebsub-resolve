; HebSub — Windows installer.
;
; Turns the PyInstaller onedir output into one downloadable .exe. Compile with
; packaging/build.py, which sets HebSubSrc to the freshly built dist folder.
;
; Two things this does beyond copying files, and both are the reason an
; installer exists at all rather than a zip:
;
;   * registers the entry in Resolve's Workflow > Scripts menu, by running the
;     app's own --install-menu. The app knows where Resolve looks; the
;     installer should not have to.
;   * removes that entry on uninstall, so an uninstalled app does not leave a
;     menu item that launches nothing.
;
; It deliberately does NOT bundle the ASR model. That is ~1.5 GB, it belongs to
; someone else, and it is fetched on first use into the user's HuggingFace
; cache where a second install can reuse it.

#ifndef HebSubSrc
  #define HebSubSrc "..\..\dist\HebSub"
#endif
#ifndef HebSubVersion
  #define HebSubVersion "4.0.0"
#endif

#define HebSubName "HebSub"
#define HebSubPublisher "Raz Tamari"
#define HebSubURL "https://github.com/razraz213/hebsub-resolve"

[Setup]
AppId={{8F3A9C21-4B7E-4D52-9E10-6C2B7A5F1D93}
AppName={#HebSubName}
AppVersion={#HebSubVersion}
AppVerName={#HebSubName} {#HebSubVersion}
AppPublisher={#HebSubPublisher}
AppPublisherURL={#HebSubURL}
AppSupportURL={#HebSubURL}/issues
DefaultDirName={autopf}\{#HebSubName}
DefaultGroupName={#HebSubName}
DisableProgramGroupPage=yes
LicenseFile=..\LICENSE
OutputBaseFilename=HebSub-{#HebSubVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-user by default so no UAC prompt is needed. A video editor should not
; have to be an administrator to install a subtitle tool, and the Resolve
; script folder we write to is per-user anyway.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\HebSub.exe

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "hebrew"; MessagesFile: "compiler:Languages\Hebrew.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "resolvemenu"; Description: "Add HebSub to DaVinci Resolve's Workflow > Scripts menu"; GroupDescription: "Integration:"

[Files]
Source: "{#HebSubSrc}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#HebSubName}"; Filename: "{app}\HebSub.exe"
Name: "{group}\{#HebSubName} — check installation"; Filename: "{app}\HebSub.exe"; Parameters: "--selftest"
Name: "{autodesktop}\{#HebSubName}"; Filename: "{app}\HebSub.exe"; Tasks: desktopicon

[Run]
; The app registers itself with Resolve -- host_resolve already knows every
; path Resolve searches, per platform, and duplicating that list here would
; give it a second home that could drift.
Filename: "{app}\HebSub.exe"; Parameters: "--install-menu"; Flags: runhidden; Tasks: resolvemenu; StatusMsg: "Adding HebSub to Resolve's Scripts menu..."
Filename: "{app}\HebSub.exe"; Description: "Launch HebSub"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Before the files go, while HebSub.exe still exists to run.
Filename: "{app}\HebSub.exe"; Parameters: "--remove-menu"; Flags: runhidden; RunOnceId: "RemoveResolveMenu"

[Messages]
english.WelcomeLabel2=This will install [name/ver] on your computer.%n%nHebSub adds Hebrew subtitles to a DaVinci Resolve timeline in one button. Everything runs locally — no cloud upload and no per-minute cost.%n%nRequires DaVinci Resolve Studio. The speech model (~1.5 GB) is downloaded the first time you transcribe.
