@echo off
rem Windows: runs windows\install.ps1 (the execution policy would block calling it directly).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install.ps1" %*
