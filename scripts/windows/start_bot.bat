@echo off
:: ===================================================================
:: Polymarket CopyBot - Iniciar/Actualizar (Lenovo)
:: Doble click sobre este archivo para arrancar el bot.
:: Si ya esta corriendo, descarga la imagen mas nueva y reinicia.
:: ===================================================================

cd /d %USERPROFILE%\polymarket_copybot\bot_trading

echo.
echo === Polymarket CopyBot ===
echo.
echo [1/3] Bajando ultimo codigo del repo...
git pull origin main

echo.
echo [2/3] Bajando ultima imagen Docker desde GitHub...
docker compose pull

echo.
echo [3/3] Arrancando los servicios...
docker compose up -d

echo.
echo === Bot corriendo ===
echo Dashboard: http://localhost:8000
echo.
echo Para ver logs en vivo: docker compose logs -f
echo Para parar el bot:     docker compose down
echo.
pause
