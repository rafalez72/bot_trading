@echo off
cd /d %USERPROFILE%\polymarket_copybot

:: Asegurarse de que la carpeta logs existe
if not exist logs mkdir logs

:: Verificar si hay cambios en GitHub
git fetch origin main 2>nul
git rev-parse HEAD > "%TEMP%\copybot_local.txt"
git rev-parse origin/main > "%TEMP%\copybot_remote.txt"
fc "%TEMP%\copybot_local.txt" "%TEMP%\copybot_remote.txt" >nul 2>&1

if %errorlevel%==0 (
    :: Sin cambios — salir silenciosamente
    exit /b 0
)

:: Cambios detectados — aplicar y reiniciar
echo [%date% %time%] Cambios detectados. Actualizando... >> logs\deploy.log
git pull origin main >> logs\deploy.log 2>&1
call .venv\Scripts\activate.bat
pip install -e . --quiet >> logs\deploy.log 2>&1

:: Reiniciar los servicios via Task Scheduler
schtasks /End /TN "CopyBot-Server" >nul 2>&1
schtasks /End /TN "CopyBot-Runner" >nul 2>&1
timeout /t 3 >nul
schtasks /Run /TN "CopyBot-Server" >nul 2>&1
schtasks /Run /TN "CopyBot-Runner" >nul 2>&1

echo [%date% %time%] Restart completado. >> logs\deploy.log
