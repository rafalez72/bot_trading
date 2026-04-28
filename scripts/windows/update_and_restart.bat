@echo off
:: ===================================================================
:: Polymarket CopyBot - Auto-update (Lenovo)
:: Lo corre Task Scheduler cada 5 minutos.
:: Detecta cambios en GitHub o en la imagen Docker, aplica y reinicia.
:: Notifica a Telegram cuando hay deploy.
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
    set /p NEW_SHA=<"%TEMP%\copybot_remote.txt"
    echo [%date% %time%] Codigo nuevo en repo. Pull... >> logs\deploy.log
    python scripts\notify.py "🔄 *Lenovo - Update detectado*%%0AHay commits nuevos en GitHub. Bajando..." 2>nul
    git pull origin main >> logs\deploy.log 2>&1
)

:: --- 2. Pull de la imagen Docker (chequea si hay nueva) ---
docker compose pull > "%TEMP%\docker_pull.txt" 2>&1
findstr /C:"Downloaded newer image" /C:"Pulled" "%TEMP%\docker_pull.txt" >nul
set IMAGE_CHANGED=%errorlevel%

if %IMAGE_CHANGED%==0 (
    echo [%date% %time%] Imagen nueva detectada. Reiniciando... >> logs\deploy.log
    python scripts\notify.py "🐳 *Lenovo - Imagen Docker nueva*%%0AReiniciando contenedores..." 2>nul
    docker compose up -d >> logs\deploy.log 2>&1
    if errorlevel 1 (
        python scripts\notify.py "❌ *Lenovo - Restart FALLO*%%0ARevisar logs/deploy.log en la Lenovo." 2>nul
        echo [%date% %time%] FAIL: docker compose up devolvio error >> logs\deploy.log
    ) else (
        echo [%date% %time%] Restart completado. >> logs\deploy.log
        for /f %%i in ('git rev-parse --short HEAD') do set SHORT_SHA=%%i
        python scripts\notify.py "✅ *Lenovo - Bot actualizado*%%0ACommit: %%SHORT_SHA%%%%0AContenedores reiniciados." 2>nul
    )
) else (
    if not %CODE_CHANGED%==0 (
        echo [%date% %time%] Solo cambio compose. Aplicando... >> logs\deploy.log
        docker compose up -d >> logs\deploy.log 2>&1
        for /f %%i in ('git rev-parse --short HEAD') do set SHORT_SHA=%%i
        python scripts\notify.py "✅ *Lenovo - docker-compose actualizado*%%0ACommit: %%SHORT_SHA%%" 2>nul
    )
)

exit /b 0
