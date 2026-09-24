@echo off

:: Pasta onde está o projeto Python
cd /d "C:\Users\bcprio\OneDrive - BCP Securities\Documentos\Market Update Automation"

:: Abre o navegador após 3 segundos
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:5000/"

:: Ativa o ambiente virtual
call ".venv\Scripts\activate.bat"

:: Executa o Flask
python app.py

:: Mantém a janela aberta se o Python terminar
pause