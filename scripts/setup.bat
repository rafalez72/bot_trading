@echo off
REM Setup portable para Windows. Crea .venv, instala deps, copia .env.
setlocal

cd /d "%~dp0\.."

where python >nul 2>nul
if errorlevel 1 (
    echo ERROR: Python no esta instalado o no esta en el PATH.
    echo Descargalo de https://www.python.org/downloads/ y marca "Add to PATH".
    exit /b 1
)

if not exist ".venv\" (
    echo - Creando entorno virtual...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

echo - Actualizando pip...
python -m pip install --quiet --upgrade pip

echo - Instalando dependencias...
pip install --quiet -e ".[analytics,dashboard]"

if not exist ".env" (
    copy .env.example .env >nul
    echo - .env creado desde .env.example
)

echo - Inicializando base SQLite...
python copybot.py init

echo.
echo Setup completo.
echo.
echo Comandos:
echo   .venv\Scripts\activate.bat
echo   python copybot.py markets
echo   python copybot.py discover
echo   python copybot.py status

endlocal
