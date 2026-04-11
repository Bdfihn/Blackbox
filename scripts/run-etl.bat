@echo off
:: Wait for Docker Desktop to be fully up
:waitfordocker
docker info >nul 2>&1
if errorlevel 1 (
    timeout /t 5 /nobreak >nul
    goto waitfordocker
)

:: Start Ollama and wait until healthy
docker compose -f "C:\Users\Bdfihn\Code\AI\docker-compose.yml" up -d --wait

:: Start Qdrant and wait until healthy
docker compose -f "C:\Users\Bdfihn\Code\Blackbox\docker-compose.yml" up -d qdrant --wait

:: Run ETL (exits when done)
docker compose -f "C:\Users\Bdfihn\Code\Blackbox\docker-compose.yml" run --rm etl
