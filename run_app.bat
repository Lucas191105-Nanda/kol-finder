@echo off
setlocal
cd /d "%~dp0"
title IGI Academy - KOL Finder

rem Cari Python: launcher "py" dulu, lalu "python"
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo.
    echo [ERROR] Python tidak ditemukan.
    echo Install Python 3.9 atau lebih baru dari https://www.python.org/downloads/
    echo Saat install, centang "Add Python to PATH", lalu jalankan file ini lagi.
    echo.
    pause
    exit /b 1
)

if not exist "app.py" (
    echo.
    echo [ERROR] app.py tidak ditemukan di folder ini: %cd%
    echo Pastikan run_app.bat berada satu folder dengan app.py.
    echo.
    pause
    exit /b 1
)

echo Menjalankan KOL Finder... Tekan Ctrl+C untuk berhenti.
echo.
%PY% run_app.py
set "CODE=%errorlevel%"

if not "%CODE%"=="0" (
    echo.
    echo [ERROR] Aplikasi berhenti dengan kode %CODE%. Cek pesan error di atas.
)
echo.
pause
exit /b %CODE%
