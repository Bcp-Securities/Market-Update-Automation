@echo off
REM ===========================================================================
REM Script para rodar os ETLs em background
REM usando o ambiente virtual
REM ===========================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

set PYTHON_VENV="%~dp0.venv\Scripts\python.exe"

set SCRIPT_1=updates_weekly\update_dim_security.py
set SCRIPT_2=updates_weekly\update_fact_pricing.py

set LOCKFILE="%~dp0run_fact_update.lock"
set LOGDIR=%~dp0logs
set RUNLOG=%LOGDIR%\run_fact_update.log

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo. >> "%RUNLOG%"
echo [%date% %time%] ========================================================== >> "%RUNLOG%"
echo [%date% %time%] === Iniciando run_fact_update.bat === >> "%RUNLOG%"
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
REM ETL 1
REM ===========================================================================

echo [%date% %time%] Iniciando %SCRIPT_1%... >> "%RUNLOG%"

%PYTHON_VENV% "%~dp0%SCRIPT_1%" < NUL > NUL 2>&1
set EXITCODE_1=%ERRORLEVEL%

echo [%date% %time%] %SCRIPT_1% retornou EXITCODE=%EXITCODE_1% >> "%RUNLOG%"

if %EXITCODE_1% NEQ 0 (
    echo [%date% %time%] ERRO: %SCRIPT_1% terminou com erro. >> "%RUNLOG%"

    del %LOCKFILE% >nul 2>&1

    echo [%date% %time%] Lock removido. Execucao encerrada com erro. >> "%RUNLOG%"

    endlocal
    exit /b %EXITCODE_1%
)

echo [%date% %time%] %SCRIPT_1% terminou com sucesso. >> "%RUNLOG%"


REM ===========================================================================
REM ETL 2
REM ===========================================================================

echo [%date% %time%] Iniciando %SCRIPT_2%... >> "%RUNLOG%"

%PYTHON_VENV% "%~dp0%SCRIPT_2%" < NUL > NUL 2>&1
set EXITCODE_2=%ERRORLEVEL%

echo [%date% %time%] %SCRIPT_2% retornou EXITCODE=%EXITCODE_2% >> "%RUNLOG%"

if %EXITCODE_2% NEQ 0 (
    echo [%date% %time%] ERRO: %SCRIPT_2% terminou com erro. >> "%RUNLOG%"

    del %LOCKFILE% >nul 2>&1

    echo [%date% %time%] Lock removido. Execucao encerrada com erro. >> "%RUNLOG%"

    endlocal
    exit /b %EXITCODE_2%
)

echo [%date% %time%] %SCRIPT_2% terminou com sucesso. >> "%RUNLOG%"


REM ===========================================================================
REM FINALIZACAO
REM ===========================================================================

del %LOCKFILE% >nul 2>&1
echo [%date% %time%] Lock removido. Fim da execucao. >> "%RUNLOG%"

echo [%date% %time%] ========================================================== >> "%RUNLOG%"
echo [%date% %time%] === Execucao finalizada com sucesso === >> "%RUNLOG%"
echo [%date% %time%] ========================================================== >> "%RUNLOG%"

endlocal
exit /b 0