# CLAUDE.md

## Core Principles
- Doing it right is better than doing it fast. You are not in a rush. NEVER skip steps or take shortcuts.
- Prefers direct, honest feedback over diplomacy.
- Speak up about bad ideas, don't just go along with them.
- When the same pattern appears in two or more places and its shape is clear, extract it into a named abstraction. Don't abstract speculatively — repetition is the signal, not anticipation of it.

## Git Workflow
- One commit per meaningful change, pushed immediately. All tests must pass before committing.
- Always use `git add .` instead of staging individual files. If there was an unexpected change, understand it and write the commit message accordingly.

## Rules
- Clean up after yourself. Temp files, spike scripts, test artifacts, and anything else created during a session shouldn't persist after completing a task. If you made it to explore or debug, delete it when done.
- Verify the latest stable version before adding any language, library, or framework dependency. Prefer it unless there's a specific reason not to.
- Don't reengineer everything from scratch. Study what already exists. Search the codebase for existing utility functions, service patterns, and UI components before writing anything new.
- When creating or modifying ignore files, audit the actual file tree, don't guess.
- Before solving any unknown (token limits, PDF parsing edge cases, etc.), run a spike first. Don't pre-solve problems you don't know you have.
- Never add comments about changes or history. Comments explain WHAT or WHY, never "improved", "better", "new", or what used to be there. 
- Match surrounding code style - consistency within a file and repository trumps external standards. 
- Confirm before selecting which LLM model to use, and before setting model parameters. I care a lot about model selection and configuration. 

## Environment & Shell
- Host OS: Windows 11
- Primary Shell: PowerShell / CMD
- Container OS: Linux (Alpine)
- Everything runs in Docker. There is no Node, python, or anything else installed on the host machine. All commands must be run inside the container

## Project Context
Blackbox is a personal life transcript generator. The goal is a complete, queryable record of how the I actually spend my time — no manual input, fully automatic, synced nightly. It ingests activity from all available data streams (PC, iPhone, wearables, audio), stores everything as searchable vectors with timestamped transcript-style logs, and lets the user ask natural language questions about their own history. It also auto-generates daily diary entries synthesized across all sources.