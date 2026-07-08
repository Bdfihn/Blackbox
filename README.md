# Blackbox

Personal life search engine. Runs 100% locally on your PC — no cloud, no manual input.

Logs everything (PC activity, iPhone, wearables, audio), embeds it into a local vector store nightly, and lets you query your own history in natural language. AI auto-generates a daily diary entry.

## Data sources

| Source | What it captures |
|---|---|
| ActivityWatch | Active PC window time, clipped to not-AFK intervals |
| Git | Commits across all local repos |
| Claude Code | Session transcripts, summarized by a local LLM |
| iPhone backup | Apple Health (sleep stages, workouts, vitals) and social interactions |

## Architecture

Three Docker services (`docker-compose.yml`):

- **qdrant** — vector store for activity chunks
- **etl** — batch job: pulls every source for one logical day (04:00 → 04:00), embeds chunks with `nomic-embed-text` via Ollama, upserts to Qdrant, and writes `diary/YYYY-MM-DD.md` with `gemma4:e4b`
- **query** — Flask RAG API and web UI at http://localhost:8080

Ollama runs in its own container on the host. The ETL is idempotent: re-running a date replaces that date's chunks and diary entry.

## Running

```
docker compose up -d qdrant --wait
docker compose run --rm etl                          # yesterday
docker compose run --rm -e ETL_DATE=YYYY-MM-DD etl   # specific date
docker compose up -d query                           # web UI
```

## Configuration

Secrets live in an untracked `.env`:

- `IPHONE_BACKUP_PASSWORD` — password for the encrypted iOS backup
- `SELF_PHONE` — own number, filtered out of social contact lists

Host paths for the iPhone backup, git repos, and Claude Code transcripts are mounted read-only in `docker-compose.yml`.

## Tests

```
docker build -t blackbox-etl-test -f etl/Dockerfile.test etl && docker run --rm blackbox-etl-test
docker build -t blackbox-query-test -f query/Dockerfile.test query && docker run --rm blackbox-query-test
```
