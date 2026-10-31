@echo off
chcp 65001 >nul
title Xiaofeiyu PC - Stop
echo Stopping Xiaofeiyu PC ...
for %%P in (17800 17801 17802 17803 17808 17809 17810) do (
    powershell -NoProfile -Command "try{Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:%%P/api/quit' -TimeoutSec 2 | Out-Null; Write-Host '  stopped on port %%P'}catch{}" 2>nul
)
echo Done. You can close this window.
timeout /t 2 >nul
