@echo off
cd /d "%~dp0"
powershell -ExecutionPolicy Bypass -File "scripts\auto_publish.ps1" %*
