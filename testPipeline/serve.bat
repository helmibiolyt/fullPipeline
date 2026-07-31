@echo off
REM Start or stop the ask page on http://localhost:8080
REM
REM     serve            start it, in this window - Ctrl+C stops it
REM     serve stop       stop whatever is holding port 8080
REM     serve restart    stop then start
REM
REM It runs in the FOREGROUND on purpose. Started with nohup or a detached
REM background job it survives the terminal, ignores Ctrl+C, and the only way
REM to stop it is to hunt the PID - which is exactly the confusion this exists
REM to avoid.

setlocal
cd /d "%~dp0\.."

if /I "%~1"=="stop"    goto :stop
if /I "%~1"=="restart" goto :restart
goto :start

:stop
echo Looking for a listener on port 8080...
set FOUND=
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8080" ^| findstr LISTENING') do (
    echo   stopping PID %%p
    taskkill /PID %%p /F >nul 2>&1
    set FOUND=1
)
if not defined FOUND echo   nothing was listening on 8080
goto :eof

:restart
call "%~f0" stop
timeout /t 2 /nobreak >nul
goto :start

:start
REM Refuse to start a second copy: uvicorn would fail to bind and the error is
REM easy to miss in the scrollback.
netstat -ano | findstr ":8080" | findstr LISTENING >nul 2>&1
if not errorlevel 1 (
    echo Port 8080 is already in use. Run "serve stop" first, or "serve restart".
    exit /b 1
)
echo.
echo   http://localhost:8080
echo   Ctrl+C to stop.
echo.
python testPipeline\serve.py
