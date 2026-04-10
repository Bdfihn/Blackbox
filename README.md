# Blackbox — Phase 1

Personal life search engine. Runs 100% locally on your PC.

## What this does
- Logs all PC activity via ActivityWatch
- Chunks and embeds it into a local Qdrant vector store nightly
- Lets you query your own history in natural language via Gemma 4
- Auto-generates a daily diary entry

## Prerequisites
- Docker Desktop with WSL2 backend (Windows 11)
- ActivityWatch installed natively on Windows (activitywatch.net)
- NVIDIA Container Toolkit for GPU passthrough

## Setup

### 1. NVIDIA Container Toolkit (for Ollama GPU)
Follow: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html
For Docker Desktop on Windows, enable GPU support in Settings → Resources → GPU.

### 2. Pull models (first time only)
```bash
# Start Ollama first
docker compose up -d ollama

# Pull models (this downloads ~15GB total, do once)
docker exec -it blackbox-ollama ollama pull gemma4:e4b
docker exec -it blackbox-ollama ollama pull gemma4:27b
docker exec -it blackbox-ollama ollama pull nomic-embed-text
```

### 3. Set your backup password
Edit `docker-compose.yml` and set:
```
IMAZING_BACKUP_PASSWORD=your_actual_password
```

### 4. Start everything
```bash
docker compose up -d
```

### 5. Open the UI
http://localhost:8080

## Services
| Service | Port | Purpose |
|---|---|---|
| Qdrant | 6333 | Vector store |
| Ollama | 11434 | Local LLM inference |
| ETL | — | Nightly data pipeline |
| Query UI | 8080 | Web interface |

## ETL schedule
Runs automatically at 02:00 every night.
Also runs once immediately on container startup.

To run manually:
```bash
docker exec -it blackbox-etl python etl.py
```

## File structure
```
blackbox/
├── docker-compose.yml
├── diary/          ← Auto-generated .md diary entries
├── data/           ← SQLite tracking DB
├── etl/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── etl.py          ← Core ETL logic
│   └── scheduler.py    ← Nightly cron
└── query/
    ├── Dockerfile
    ├── requirements.txt
    ├── rag.py          ← RAG search logic
    ├── server.py       ← Flask API + static server
    └── static/
        └── index.html  ← Web UI
```

## Phase 2 (coming next)
- Garmin FIT file parser
- iPhone Screen Time extraction via iOSbackup
- Audio transcription via WhisperX
- Cross-source timestamp joining
