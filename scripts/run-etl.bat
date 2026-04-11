@echo off
:: Start Ollama if not already running
docker compose -f "C:\Users\Bdfihn\Code\AI\docker-compose.yml" up -d

:: Start Qdrant
docker compose -f "C:\Users\Bdfihn\Code\Blackbox\docker-compose.yml" up -d qdrant

:: Wait for services to be ready
timeout /t 15 /nobreak >nul

:: Run ETL (exits when done)
docker compose -f "C:\Users\Bdfihn\Code\Blackbox\docker-compose.yml" run --rm etl
