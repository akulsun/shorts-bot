# NOTES — Shorts Bot

Living status doc. **A new session knows only what is written here.**
Full design and rationale: `docs/PLAN.md`.

Last updated: 2026-10-31

## Goal

Turn a YouTube URL or a local video into 20–60 s vertical 1080x1920 MP4 clips
with burned-in word-level captions. Free, local, CPU-only, no paid APIs.

## Environment (verified)

- Windows, Ryzen 4500U, 8 GB RAM, no GPU
- Python **3.14.8** — works; all deps have `cp314` wheels
- ffmpeg 9.0.2 (gyan "essentials" build) via Chocolatey, on PATH
  - has `libx264`, `aac`, and the `ass` filter
- venv at `venv/` (gitignored). Recreate with:
  `python -m venv venv` then `venv\Scripts\python.exe -m pip install -r requirements.txt`

## Decisions locked in

- Whisper default is `small`; `config.yaml` currently sets **`base`** while
  testing stages 2–4. Remember to flip to `small` before real use.
- `render.crop_mode: blur_pad` is the default (`center` kept as an option).
- `selection.max_clips: 3`.
- Clip picking sits behind a `Selector` protocol (`selection.selector`) so an
  LLM can replace the heuristic without touching the pipeline.
- Cut + vertical + captions are **one** ffmpeg pass (one encode, not three).

## Architecture

`main.py` → `shortsbot/cli.py` subcommands. Each stage reads and writes files
under `temp/<video_id>/` and records status in `state.json`, so any stage can
be re-run alone. `transcript.json` and `clips.json` are caches. Final MP4s go
to `output/`.

Stage subcommands are registered in `cli.py` only once their module exists, so
the CLI never advertises something that does not work.

## Stage list

| # | Stage | Module(s) | Status |
|---|---|---|---|
| 0 | Foundation: config, models, ffmpeg wrapper, state, CLI, `check-env` | `config.py`, `models.py`, `ffmpeg.py`, `state.py`, `logging_setup.py`, `checks.py`, `cli.py` | **done** |
| 1 | Download + audio extraction | `download.py`, `audio.py` | **todo** |
| 2 | Transcribe (faster-whisper) | `transcribe.py` | **todo** |
| 3 | Clip selection | `selectors/base.py`, `selectors/heuristic.py` | **todo** |
| 4 | Render (cut + 9:16 + captions) | `captions.py`, `render.py` | **todo** |
| 5 | Batch / end-to-end | `pipeline.py` | **todo** |

## Run commands

```
venv\Scripts\python.exe main.py check-env
venv\Scripts\python.exe main.py check-env --smoke-whisper
```

`check-env` verifies: ffmpeg, ffprobe, libx264/aac, `ass` filter, yt-dlp,
faster-whisper, config validity, writable folders, Windows filterpath
escaping. `--smoke-whisper` additionally loads the configured model and
decodes 3 s of generated audio (first run downloads the model once).

Planned (not built yet):

```
python main.py download <url-or-path>
python main.py transcribe <video_id> [--preview N] [--force]
python main.py select <video_id> [--top N]
python main.py render <video_id> --clip <index>
python main.py run <url-or-path> [--clips N]
```

## Module notes (for the next session)

- `config.py` — `load_config(path=None) -> Config`. Every tunable comes from
  `config.yaml`; relative paths resolve against the config file's folder.
  **Unknown keys are a hard error** (typo protection). `ensure_directories()`.
- `models.py` — `Word`, `Segment`, `Transcript` (+ `words_between`,
  `text_between`), `Clip`. All have `to_dict()` / `from_dict()` for caching.
  Timestamps are seconds (float).
- `ffmpeg.py` — `run_ffmpeg(args, desc=...)` (adds `-y -loglevel error`,
  raises `FFmpegError` with the last 25 stderr lines), `ffmpeg_output()`,
  `probe(path) -> MediaInfo(duration, width, height, fps, has_audio)`,
  `has_encoder()`, `has_filter()`, `escape_filter_path(path)`.
  **Note:** `-nostdin` is passed to ffmpeg only — ffprobe 9.0 rejects it.
- `state.py` — `State.for_source(cfg, url_or_path)` / `State.load(cfg, vid)`.
  `mark_done()`, `require(stage)`, `get()/set()` persist to
  `temp/<video_id>/state.json`. `source_id()` = sha1 of the normalized
  source, first 10 chars.
- `checks.py` — diagnostics for `check-env`; each check returns
  `(ok, detail, hint)` and never raises.
- `errors.py` — `ShortsBotError` base; CLI prints these without a traceback.

## Known issues / gotchas

1. **PyAV pinned to 16.1.0.** faster-whisper 1.2.1 calls
   `av.open(..., metadata_errors=...)`, removed in PyAV 17. Do not bump `av`
   past 16 until faster-whisper is updated.
2. **ffprobe rejects `-nostdin`** in ffmpeg 9.x ("Option not found"). Only
   ffmpeg gets that flag; handled in `ffmpeg._run`.
3. **First transcription downloads the model** (~145 MB for `base`,
   ~460 MB for `small`) into `%USERPROFILE%\.cache\huggingface`.
4. **HF symlink warning** on Windows is harmless (no Developer Mode).
5. **ffmpeg `ass=` on Windows** needs `C\:/path/x.ass`; use
   `ffmpeg.escape_filter_path()` — never build this by hand.
6. **YouTube bot checks** — keep yt-dlp updated; `download.cookies_from_browser`
   is the escape hatch. Not implemented yet.
7. **Speed** (measured, `base`/int8 on this CPU): model load 1.3 s, 3 s of
   audio decoded in ~1.8 s. Expect roughly real-time-ish for `base` and
   slower for `small`.

## Next action

Stage 0 is committed and awaiting the user's test. **Do not start Stage 1
until the user confirms.** Next up: `download.py` (yt-dlp + local files) and
`audio.py` (16 kHz mono WAV).
