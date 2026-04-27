@echo off
:: ===================================================================
:: Polymarket CopyBot - Auto-update (Lenovo)
:: Lo corre Task Scheduler cada 5 minutos.
:: Detecta cambios en GitHub o en la imagen Docker, aplica y reinicia.
:: ===================================================================

cd /d %USERPROFILE%\polymarket_copybot\bot_trading

if not exist logs mkdir logs

:: --- 1. Pull del codigo (docker-compose.yml puede haber cambiado) ---
git fetch origin main 2>nul
git rev-parse HEAD > "%TEMP%\copybot_local.txt"
git rev-parse origin/main > "%TEMP%\copybot_remote.txt"
fc "%TEMP%\copybot_local.txt" "%TEMP%\copybot_remote.txt" >nul 2>&1
set CODE_CHANGED=%errorlevel%

if not %CODE_CHANGED%==0 (
    echo [%date% %time%] Codigo nuevo en repo. Pull... >> logs\deploy.log
    git pull origin main >> logs\deploy.log 2>&1
)

:: --- 2. Pull de la imagen Docker (chequea si hay nueva) ---
docker compose pull --quiet > "%TEMP%\docker_pull.txt" 2>&1
findstr /C:"Downloaded newer image" /C:"Pulled" "%TEMP%\docker_pull.txt" >nul
set IMAGE_CHANGED=%errorlevel%

if %IMAGE_CHANGED%==0 (
    echo [%date% %time%] Imagen nueva detectada. Reiniciando... >> logs\deploy.log
    docker compose up -d >> logs\deploy.log 2>&1
    echo [%date% %time%] Restart completado. >> logs\deploy.log
) else (
    if not %CODE_CHANGED%==0 (
        echo [%date% %time%] Solo cambio compose. Aplicando... >> logs\deploy.log
        docker compose up -d >> logs\deploy.log 2>&1
    )
)

exit /b 0
