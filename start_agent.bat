@echo off
chcp 65001 >nul
title Start-up Daily CF Report
cd /d "%~dp0"

if not exist inbox mkdir inbox

echo.
echo  =================================================
echo    inbox 폴더에 은행 파일(엑셀/CSV)을 넣어주세요.
echo    Drop your bank file into the inbox folder.
echo  =================================================
echo.

start "" explorer "inbox"

where python >nul 2>nul
if errorlevel 1 (
    echo  Python이 설치되어 있지 않습니다. https://python.org 에서 설치 후 다시 실행해 주세요.
    echo  Python is not installed. Install it from https://python.org and run this again.
    pause >nul
    exit /b 1
)

echo  준비 중입니다... (Preparing...)
python -m pip install -q -r requirements.txt

python watch_inbox.py %*
pause >nul
