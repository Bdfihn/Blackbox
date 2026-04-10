# CLAUDE.md

## Core Principles
- Doing it right is better than doing it fast. You are not in a rush. NEVER skip steps or take shortcuts.
- Prefers direct, honest feedback over diplomacy.
- Speak up about bad ideas, don't just go along with them.
- When the same pattern appears in two or more places and its shape is clear, extract it into a named abstraction. Don't abstract speculatively — repetition is the signal, not anticipation of it.

## Git Workflow
- After every meaningful change, commit and push. Don't let work pile up uncommitted.
- Never commit broken code — one commit per completed task. 
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
Blackbox is a personal life transcript generator. The goal is a complete, queryable record of how the user actually spends their time — no manual input, fully automatic, synced nightly. It ingests activity from all available data streams (PC, iPhone, wearables, audio), stores everything as searchable vectors with timestamped transcript-style logs, and lets the user ask natural language questions about their own history. It also auto-generates daily diary entries synthesized across all sources.

### What
A local-first, private ETL + RAG pipeline. Data is captured from ActivityWatch (PC), iPhone Health backups, and future sources (Garmin, Omi). Each night the pipeline chunks and embeds all new data into Qdrant, writes a Gemma-generated diary entry to `/diary/`, and keeps a SQLite deduplication log. A Flask query service exposes a RAG interface — embed the question, retrieve top chunks, answer with Gemma. Everything runs in Docker on the user's local machine. Nothing leaves the network.

### How
- **Capture**: ActivityWatch (always-on Windows service), iPhone backup via Apple Devices app (nightly USB), Smartwatch, Audio files and transcripts
- **Process**: Nightly ETL in `etl/etl.py` — pulls AW events via local API, decrypts iPhone backup via `iOSbackup`, chunks all sources into 5-min windows normalized to local timezone, embeds via `nomic-embed-text` through Ollama
- **Store**: Qdrant (vectors + metadata), SQLite (ingestion tracking), `/diary/*.md` (human-readable)
- **Query**: `query/rag.py` — semantic search over Qdrant, answer generation via `gemma4:e4b`, served at `localhost:8080`
- **Stack**: Python 3.12, Docker Compose, Qdrant, Ollama (gemma4:e4b + gemma4:27b + nomic-embed-text), Flask, ActivityWatch, iOSbackup