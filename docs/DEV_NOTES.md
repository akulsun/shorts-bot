# Developer Notes — Shorts Bot

This document collects architectural decisions, Windows-specific behaviors, and engineering gotchas for developers working on Shorts Bot.

---

## 1. Architecture & Design Decisions

* **Pipeline Flow**: `download` → `audio` (16 kHz mono WAV) → `transcribe` (faster-whisper) → `select` (heuristic or swappable selector) → `render` (one-pass vertical encode with burned-in ASS captions).
* **Work Folder Isolation (`temp/<video_id>/`)**:
  - Each source (URL or local path) generates a stable 10-hex-character ID (`source_id()` based on SHA-1).
  - Every stage records its state, metadata, and cache paths in `state.json`.
  - Stages can be executed independently from CLI, and previous stages will reuse cached outputs if inputs have not changed.
* **One-Pass FFmpeg Render**:
  - Cut (`-ss` / `-t`), aspect ratio conversion (center crop or blurred background padding), and caption burn-in are executed in a **single ffmpeg invocation**.
  - Avoiding intermediate intermediate encodes saves significant CPU time and disk I/O.
* **Swappable Selection Interface**:
  - `selectors/base.py` defines the `Selector` protocol (`select(transcript, cfg) -> SelectionOutcome`).
  - `HeuristicSelector` scores candidates based on speech density, keywords, hook presence, sentence completeness, filler words, and pauses.
  - Adding an LLM or alternative selector only requires creating a class implementing `Selector` and registering it in `shortsbot/selectors/__init__.py`.
* **Render Caching**:
  - `state.json` caches render results with a settings signature (clip bounds, render settings, caption settings).
  - Running the pipeline again reuses existing `.mp4` outputs if settings have not changed, while re-rendering only invalidated clips.
* **Conservative Cleanup (`--clean`)**:
  - Downloaded source videos and extracted WAV files are deleted only after **every selected clip renders successfully**.
  - Partial failures or user cancellations keep temporary source files so re-runs do not require re-downloading.
  - Clean routines strictly refuse to delete files outside designated work/download folders and never touch user-supplied local input files.

---

## 2. Windows Quirks & Subprocess Gotchas

* **Chocolatey / Shim Process Trees**:
  - On Windows, Chocolatey's `ffmpeg.exe` is often a shim process that spawns the real binary.
  - Calling `process.terminate()` terminates only the shim, leaving the underlying `ffmpeg.exe` running and its standard I/O pipes open.
  - Shorts Bot uses `taskkill /PID <pid> /T /F` on Windows to cleanly terminate the entire child process tree during cancellations. On POSIX, `start_new_session=True` with `os.killpg()` is used.
* **FFmpeg ASS Filter Paths & Escaping**:
  - FFmpeg's filtergraph parser cannot reliably escape single quotes `'` inside filter option values (e.g. `ass=filename='...'`).
  - To avoid path parsing bugs on paths containing spaces, colons, or apostrophes, caption files are written to safe relative paths (`captions/clip_00.ass`), and FFmpeg is executed with `cwd=state.workdir`.
* **Windows File Locking (`PermissionError`)**:
  - Antivirus scanners or concurrent media players can momentarily lock newly created media files.
  - File deletion routines catch `PermissionError`, log a warning, and proceed cleanly without crashing the batch.
* **Explorer Selection (`explorer /select,<path>`)**:
  - `explorer.exe` treats commas as option delimiters. If a path contains commas, invoking `/select` can misparse arguments.
  - When opening containing folders for files whose path contains commas, the app falls back to `os.startfile(folder)`.

---

## 3. Dependencies & Runtime Gotchas

* **PyAV Version Pinning**:
  - `faster-whisper==1.2.1` relies on `av.open(..., metadata_errors=...)`.
  - PyAV v17 removed `metadata_errors`. `av` is therefore pinned to `16.1.0` in `requirements.txt`.
* **FFprobe Flag Compatibility**:
  - FFprobe 9.x rejects `-nostdin` ("Option not found"). Only FFmpeg receives `-nostdin`.
* **yt-dlp JavaScript Runtime**:
  - YouTube extraction may log warnings when no external JS runtime (Node/Deno) is detected on PATH. Installing `deno` (`winget install DenoLand.Deno`) resolves this if YouTube extraction fails.
* **Whisper Model Caching**:
  - Models (`base`, `small`, etc.) are downloaded at runtime from Hugging Face (`Systran/faster-whisper-*`) and cached in `%USERPROFILE%\.cache\huggingface`.
  - The model instance is explicitly freed with `gc.collect()` after transcription to keep RAM flat on systems with <= 8 GB memory.
