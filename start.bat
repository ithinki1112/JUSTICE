@echo off
chcp 65001 > nul

where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set PYTHON=py
) else (
    where python >nul 2>&1
    if %ERRORLEVEL% EQU 0 ( set PYTHON=python ) else (
        echo Python이 설치되어 있지 않습니다. setup.bat을 먼저 실행하세요.
        pause & exit /b 1
    )
)

echo JUSTICE 시작 중...
echo 브라우저에서 http://localhost:5000 으로 접속하세요
echo 종료하려면 이 창을 닫으세요.
echo.
start "" http://localhost:5000
%PYTHON% app.py
pause
