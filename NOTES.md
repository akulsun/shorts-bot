# NOTES — Shorts Bot

Living status doc. **A new session knows only what is written here.**

Last updated: 2026-10-31 (project start, plan approved? **NO — awaiting approval**)

## Goal

Turn a YouTube URL or a local video into 20–60 s vertical 1080x1920 MP4 clips
with burned-in word-level captions. Free, local, CPU-only, no paid APIs.

## Environment

- Windows, Ryzen 4500U, 8 GB RAM, no GPU
- Python **3.14.8** (only interpreter on PATH — plan assumes 3.11+, 3.14 is
  unverified for faster-whisper until Stage 0 confirms it)
- ffmpeg 9.0.2 (gyan build) with libx264 / libass / libfreetype — OK
- Not a git repo before this commit.

## Architecture (one paragraph)

`main.py` → `shortsbot/cli.py` subcommands. Each stage reads and writes files
under `temp/<video_id>/` and records status in `state.json`, so any stage can
be re-run alone. `transcript.json` and `clips.json` are caches. Clips land in
`output/`. Clip picking is behind a `Selector` protocol so a heuristic or an
LLM can be swapped via `selection.selector` in `config.yaml`.

## Stage list

| # | Stage | Module(s) | Status |
|---|---|---|---|
| 0 | Foundation (config, models, ffmpeg wrapper, CLI) | `config.py`, `models.py`, `ffmpeg.py`, `state.py`, `cli.py` | **todo** |
| 1 | Download + audio extraction | `download.py`, `audio.py` | **todo** |
| 2 | Transcribe (faster-whisper) | `transcribe.py` | **todo** |
| 3 | Clip selection | `selectors/` | **todo** |
| 4 | Render (cut + 9:16 + captions) | `captions.py`, `render.py` | **todo** |
| 5 | Batch / end-to-end | `pipeline.py` | **todo** |

Status values: todo / in progress / done.

Full design and rationale: `docs/PLAN.md`.

## Run commands

None yet — nothing is implemented.

Planned interface:

```
python main.py check-env
python main.py download <url-or-path>
python main.py transcribe <video_id> [--preview N] [--force]
python main.py select <video_id> [--top N]
python main.py render <video_id> --clip <index>
python main.py run <url-or-path> [--clips N]
```

## Conventions

- No hardcoded paths — everything through `config.yaml`.
- ffmpeg/yt-dlp failures raise a typed error with the tail of stderr logged.
- One clip rendered at a time (RAM budget).
- One stage = one commit, only after the user confirms it works.

## Known issues / risks

- **Python 3.14**: `ctranslate2 4.8.4` + `onnxruntime 1.31.1` publish cp314
  win wheels, so install should work. Confirm in Stage 0 (whisper smoke test).
  If it breaks, install Python 3.12 and recreate `venv/`.
- **First transcription downloads the model** (~460 MB for `small`).
- **ffmpeg `ass=` filter on Windows** needs escaped drive colons
  (`C\:/x/y.ass`); handled by a shared helper once implemented.
- **YouTube bot checks** — keep yt-dlp updated; `cookies_from_browser` config
  key is the escape hatch.
- **Speed**: `small`/int8 on this CPU is roughly 15–40 min per hour of video.

## Next action

Waiting for user approval of `docs/PLAN.md` (module layout + defaults).
On approval: build Stage 0 only, then stop for a manual test.
