@echo off
chcp 65001 >nul
title Xiaofeiyu PC - Launcher
cd /d "%~dp0"

rem 1) common install locations
for %%D in (
  "%LOCALAPPDATA%\Programs\Python\Python313"
  "%LOCALAPPDATA%\Programs\Python\Python312"
  "%LOCALAPPDATA%\Programs\Python\Python311"
  "%LOCALAPPDATA%\Programs\Python\Python310"
  "%LOCALAPPDATA%\Programs\Python\Python39"
  "C:\Python313"
  "C:\Python312"
  "C:\Python311"
  "C:\Python310"
) do (
  if exist "%%~D\pythonw.exe" (
    start "" "%%~D\pythonw.exe" "%~dp0app.py" --open
    goto :eof
  )
)

rem 2) pythonw / python on PATH
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0app.py" --open
    goto :eof
)
where python >nul 2>nul
if %errorlevel%==0 (
    start "" python "%~dp0app.py" --open
    goto :eof
)

echo.
echo  [!] Python not found. This app needs Python 3.9+ (no third-party packages).
echo      https://www.python.org/downloads/  (tick "Add python.exe to PATH")
echo      Then run this file again.
echo.
pause
