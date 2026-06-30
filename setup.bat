@echo off
chcp 65001 > nul
echo ===================================
echo  JUSTICE 설치 시작
echo ===================================

:: Python 설치 확인
where py >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    set PYTHON=py
    goto :python_found
)
where python >nul 2>&1
if %ERRORLEVEL% EQU 0 (
    python --version >nul 2>&1
    if %ERRORLEVEL% EQU 0 (
        set PYTHON=python
        goto :python_found
    )
)
echo.
echo [오류] Python이 설치되어 있지 않습니다.
echo.
echo Python 3.11 이상을 설치하세요:
echo   https://www.python.org/downloads/
echo.
echo 설치 시 [Add python.exe to PATH] 체크박스를 반드시 체크하세요!
echo.
pause
exit /b 1

:python_found
echo Python: %PYTHON%

echo.
echo [1/3] pip 패키지 설치 중...
%PYTHON% -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo 오류: pip install 실패
    pause
    exit /b 1
)

echo.
echo [2/3] Playwright 브라우저 설치 중...
%PYTHON% -m playwright install chromium
if %ERRORLEVEL% NEQ 0 (
    echo 오류: Playwright 브라우저 설치 실패
    pause
    exit /b 1
)

echo.
echo [3/3] 데이터베이스 초기화 중...
%PYTHON% -c "from database import init_db; init_db(); print('DB OK')"

echo.
echo ===================================
echo  설치 완료! start.bat 으로 실행하세요
echo ===================================
pause
