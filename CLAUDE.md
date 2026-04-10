# CLAUDE.md

## Core Principles
- Doing it right is better than doing it fast. You are not in a rush. NEVER skip steps or take shortcuts.
- Prefers direct, honest feedback over diplomacy.
- Speak up about bad ideas, don't just go along with them.

## Git Workflow
- After every meaningful change, commit and push. Don't let work pile up uncommitted.
- Never commit broken code — one commit per completed task that the user has verified as completed. Before comitting, ask the user to test the feature you built. 
- Check for committed code before starting any new plan.
- Always use `git add .` instead of staging individual files. If there was an unexpected change, understand it and write the commit message accordingly.

## Rules
- Use Context7 to verify latest stable library versions before adding to package.json.
- When working with libraries, frameworks, and APIs, use Context7 to fetch up-to-date documentation.
- Write tests that verify real observable behavior — inputs in, outputs out. Avoid tests that just assert mocks were called in the right order. All tests must pass before committing.
- When creating or modifying ignore files, audit the actual file tree, don't guess.
- Before solving any unknown (token limits, PDF parsing edge cases, etc.), run a spike first. Don't pre-solve problems you don't know you have.
- Never add comments about changes or history. Comments explain WHAT or WHY, never "improved", "better", "new", or what used to be there. 
- Match surrounding code style - consistency within a file and repository trumps external standards. 
- Don't reengineer everything from scratch. Study what already exists. Search the codebase for existing utility functions, service patterns, UI components, depending on what you're working on. 
- Confirm before selecting which LLM model to use, and before setting model parameters. I care a lot about model selection and configuration. 

## Environment & Shell
- Host OS: Windows 11
- Primary Shell: PowerShell / CMD
- Container OS: Linux (Alpine)
- Everything runs in Docker. There is no Node, npm, or anything else installed on the host machine. All commands must be run inside the container

## Project Specifics

### Why
Blackbox is a personal life logging system. It ingests activity data from the user's PC (ActivityWatch) and iPhone (encrypted backups), stores it as searchable vectors, and lets the user ask natural language questions about their own past activity. It also auto-generates daily diary entries. The goal is a complete, queryable record of how the user actually spends their time — no manual input, no app to open.

### What
**Stack:** Python, Docker, Qdrant (vector DB), SQLite (dedup tracking), Ollama (local LLMs), Flask

**Components:**
- `etl/etl.py` — Main ETL orchestrator. Fetches ActivityWatch window events, chunks into 5-min windows, embeds via `nomic-embed-text`, upserts to Qdrant, generates diary entries via `gemma4:e4b`. Also defines `upsert_chunks()` and diary generation.
- `etl/iphone.py` — Parses encrypted iOS backups. Extracts foreground app usage from `knowledgeC.db` and steps/heart rate/sleep from `healthdb_secure.sqlite`. Also owns `day_bounds()` — the canonical definition of a logical day.
- `etl/scheduler.py` — Runs ETL nightly at 04:15 (after the 04:00 day boundary).
- `query/rag.py` — Embeds a question, retrieves top-K chunks from Qdrant, answers via `gemma4:e4b`.
- `query/server.py` — Flask API. Serves the web UI and exposes `/api/query`, `/api/diary`, `/api/diary/<date>/timeline`, and diary CRUD.

**Models:** `nomic-embed-text` for embeddings, `gemma4:e4b` for generation. Confirm model selection before changing.

**Storage:** Qdrant holds vectors + payloads. SQLite tracks ingested chunk IDs to prevent re-embedding. Diary entries are plain `.md` files.

### How
- **Day boundary:** A logical "day" runs 04:00 → next day 04:00. `day_bounds()` in `iphone.py` is the single source of truth. The `date` payload field in Qdrant reflects the logical date, not the wall-clock date. Never revert to midnight-to-midnight windows.
- **iPhone backups:** Must be mounted as a Docker volume. Two paths are checked: `IPHONE_BACKUP_PATH` then `IPHONE_BACKUP_PATH2`. `knowledgeC.db` is only present in full encrypted backups (not standard Finder/iTunes backups) — if it's missing, the parser silently returns empty.
- **ActivityWatch:** Must be running on the host machine. The ETL container reaches it via `host.docker.internal:5600`. Only `aw-watcher-window` buckets are ingested.
- **Dedup:** Chunks are deduplicated by MD5 of their text. Re-running ETL for the same day is safe — already-ingested chunks are skipped. To re-ingest a day, delete it via `DELETE /api/diary/<date>` first.
- **ActivityWatch day setting:** AW's "Start of day: 04:00" setting in its own UI is set to match. This only affects AW's web UI display — our ETL constructs its own time windows and is unaffected by this setting.