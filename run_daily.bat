@echo off
REM ===========================================================================
REM Script para rodar os ETLs do Market Update em background
REM usando o ambiente virtual
REM ===========================================================================
setlocal enabledelayedexpansion

cd /d "%~dp0"

set PYTHON_VENV="%~dp0.venv\Scripts\python.exe"

set SCRIPT_MAIN=market_update.py

set LOCKFILE="%~dp0run_market_update.lock"
set LOGDIR=%~dp0logs
set RUNLOG=%LOGDIR%\run_bat.log

if not exist "%LOGDIR%" mkdir "%LOGDIR%"

echo. >> "%RUNLOG%"
echo [%date% %time%] ========================================================== >> "%RUNLOG%"
echo [%date% %time%] === Iniciando run_background.bat === >> "%RUNLOG%"
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
REM ETL PRINCIPAL
REM ===========================================================================

echo [%date% %time%] Iniciando %SCRIPT_MAIN%... >> "%RUNLOG%"

%PYTHON_VENV% "%~dp0%SCRIPT_MAIN%" < NUL > NUL 2>&1
set EXITCODE_MAIN=%ERRORLEVEL%

echo [%date% %time%] %SCRIPT_MAIN% retornou EXITCODE=%EXITCODE_MAIN% >> "%RUNLOG%"

if %EXITCODE_MAIN% NEQ 0 (
    echo [%date% %time%] ERRO: %SCRIPT_MAIN% terminou com erro. >> "%RUNLOG%"

    del %LOCKFILE% >nul 2>&1

    echo [%date% %time%] Lock removido. Execucao encerrada com erro. >> "%RUNLOG%"

    endlocal
    exit /b %EXITCODE_MAIN%
)

echo [%date% %time%] %SCRIPT_MAIN% terminou com sucesso. >> "%RUNLOG%"


REM ===========================================================================
REM FINALIZACAO
REM ===========================================================================

del %LOCKFILE% >nul 2>&1
echo [%date% %time%] Lock removido. Fim da execucao. >> "%RUNLOG%"

echo [%date% %time%] ========================================================== >> "%RUNLOG%"
echo [%date% %time%] === Execucao finalizada === >> "%RUNLOG%"
echo [%date% %time%] ========================================================== >> "%RUNLOG%"

endlocal
exit /b %EXITCODE_MAIN%
