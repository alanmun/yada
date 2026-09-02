yada - Yet Another Dictating App

TO INSTALL

  Windows:  double-click yada.exe
  Linux:    run  ./yada install

That is all. yada installs itself into your own user folder, needs no administrator
rights, adds itself to your Start Menu / application menu, and starts.

Windows may warn that the publisher is unrecognised, because these binaries are not
code-signed. Choose "More info", then "Run anyway".

On Windows 11 the tray icon starts out hidden behind the ^ arrow on the taskbar. Drag
it out, or turn on Settings > Personalisation > Taskbar > "Other system tray icons".

Then open Settings from the tray icon and paste an API key.

If something looks wrong, yada can diagnose itself:
    Windows:  %LOCALAPPDATA%\yada\versions\<version>\yada.exe doctor
    Linux:    ~/.local/share/yada/versions/<version>/yada doctor

Everything else in this folder is the application itself. You do not need to open it.
