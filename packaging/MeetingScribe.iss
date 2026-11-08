; Windows installer: per-user, no admin rights. Built from PyInstaller's dist\MeetingScribe folder:
;   ISCC /DAppVersion=1.0.0 packaging\MeetingScribe.iss
; Uninstalling removes the program and its start-at-login entry; notes and recordings stay.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{6F1C2A4E-3B7D-4E55-9C11-7A8E2D4F9B10}
AppName=MeetingScribe
AppVersion={#AppVersion}
AppPublisher=Preet Bohra
AppPublisherURL=https://meetingscribe.preetbohra.com
DefaultDirName={localappdata}\Programs\MeetingScribe
DefaultGroupName=MeetingScribe
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=MeetingScribe-{#AppVersion}-Windows-Setup
SetupIconFile=MeetingScribe.ico
UninstallDisplayIcon={app}\MeetingScribe.exe
Compression=lzma2/fast
SolidCompression=no
WizardStyle=modern
LicenseFile=..\LICENSE
CloseApplications=force
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Files]
Source: "..\dist\MeetingScribe\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\MeetingScribe"; Filename: "{app}\MeetingScribe.exe"
Name: "{userdesktop}\MeetingScribe"; Filename: "{app}\MeetingScribe.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"

[Registry]
; The app adds this value itself after setup; listed here so that uninstalling removes it.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueName: "MeetingScribe"; Flags: dontcreatekey uninsdeletevalue

[Run]
Filename: "{app}\MeetingScribe.exe"; Description: "Open MeetingScribe"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM MeetingScribe.exe"; Flags: runhidden; RunOnceId: "StopMeetingScribe"
