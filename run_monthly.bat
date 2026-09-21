@echo off
REM ===========================================================================
REM Script para rodar o ETL MENSAL do Market Update em background
REM usando o ambiente virtual
REM ===========================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

set PYTHON_VENV="%~dp0.venv\Scripts\python.exe"

set SCRIPT_MONTHLY=updates_monthly\monthly_update.py

set LOCKFILE="%~dp0run_monthly_update.lock"
set LOGDIR=%~dp0logs
set RUNLOG=%LOGDIR%\run_bat_monthly.log

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo. >> "%RUNLOG%"
echo [%date% %time%] ========================================================== >> "%RUNLOG%"
echo [%date% %time%] === Iniciando run_monthly.bat === >> "%RUNLOG%"
echo [%date% %time%] ========================================================== >> "%RUNLOG%"


REM ===========================================================================
REM TRAVA CONTRA EXECUCAO CONCORRENTE
REM ===========================================================================

if exist %LOCKFILE% (
    echo [%date% %time%] ABORTADO: lock file existente. >> "%RUNLOG%"
    exit /b 1
)

echo running > %LOCKFILE%


REM ===========================================================================
REM CHECAGEM DO AMBIENTE VIRTUAL
REM ===========================================================================

if not exist %PYTHON_VENV% (
    echo [%date% %time%] ERRO: Python do venv nao encontrado. >> "%RUNLOG%"
    del %LOCKFILE% >nul 2>&1
    exit /b 1
)


REM ===========================================================================
REM ETL MENSAL
REM ===========================================================================

echo [%date% %time%] Iniciando %SCRIPT_MONTHLY%... >> "%RUNLOG%"

%PYTHON_VENV% "%~dp0%SCRIPT_MONTHLY%" < NUL > NUL 2>&1
set EXITCODE_MONTHLY=%ERRORLEVEL%

echo [%date% %time%] %SCRIPT_MONTHLY% retornou EXITCODE=%EXITCODE_MONTHLY% >> "%RUNLOG%"

if %EXITCODE_MONTHLY% NEQ 0 (
    echo [%date% %time%] ERRO: %SCRIPT_MONTHLY% terminou com erro. >> "%RUNLOG%"
) else (
    echo [%date% %time%] %SCRIPT_MONTHLY% terminou com sucesso. >> "%RUNLOG%"
)


REM ===========================================================================
REM FINALIZACAO
REM ===========================================================================

del %LOCKFILE% >nul 2>&1
echo [%date% %time%] Lock removido. Fim da execucao. >> "%RUNLOG%"

echo [%date% %time%] ========================================================== >> "%RUNLOG%"
echo [%date% %time%] === Execucao finalizada === >> "%RUNLOG%"
echo [%date% %time%] ========================================================== >> "%RUNLOG%"

endlocal
exit /b %EXITCODE_MONTHLY%