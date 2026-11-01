# NOTES — Shorts Bot

Living status doc. **A new session knows only what is written here.**
Full design and rationale: `docs/PLAN.md`.

Last updated: 2026-11-01

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
- **No JavaScript runtime installed** (deno/node). yt-dlp still downloads, but
  warns on every run that YouTube extraction without one is deprecated.

## Decisions locked in

- Whisper default is `small`; `config.yaml` currently sets **`base`** while
  building and testing the pipeline. Remember to flip to `small` before real
  use.
- `render.crop_mode: blur_pad` is the default (`center` kept as an option).
- `selection.max_clips: 3`.
- Clip picking sits behind a `Selector` protocol (`selection.selector`) so an
  LLM can replace the heuristic without touching the pipeline.
- Cut + vertical + captions are **one** ffmpeg pass (one encode, not three).
- **Source videos are cached in `temp/<video_id>/`**, not `downloads/`.
  `paths.downloads` is therefore currently unused (it is still created and
  gitignored).

## Architecture

`main.py` → `shortsbot/cli.py` subcommands. Each stage reads and writes files
under `temp/<video_id>/` and records status in `state.json`, so any stage can
be re-run alone. Final MP4s go to `output/`.

Stage subcommands are registered in `cli.py` only once their module exists, so
the CLI never advertises something that does not work.

### Work folder layout

```
temp/<video_id>/
  state.json       # stage status + source metadata + cached paths
  source.mp4       # downloaded (local files are referenced, not copied)
  audio.wav        # 16 kHz mono PCM s16le
  transcript.json  # Transcript.to_dict(): language, duration, segments[], words[]
  clips.json       # selection output: selector, settings, reason, clips[]
  captions/
    clip_00.ass    # burned-in captions for clip rank 0 (one per rendered clip)
```

`<video_id>` = first 10 hex chars of sha1 of the normalized source
(URL string, or absolute path for local files). Same input -> same folder ->
cache hit.

## Stage list

| # | Stage | Module(s) | Status |
|---|---|---|---|
| 0 | Foundation: config, models, ffmpeg wrapper, state, CLI, `check-env` | `config.py`, `models.py`, `ffmpeg.py`, `state.py`, `logging_setup.py`, `checks.py`, `cli.py` | **done** |
| 1 | Download + audio extraction | `download.py`, `audio.py`, `formatting.py` | **done** |
| 2 | Transcribe (faster-whisper, word-level) | `transcribe.py`, `jsonio.py` | **done** |
| 3 | Clip selection | `selectors/base.py`, `selectors/heuristic.py`, `selectors/__init__.py` | **done** |
| 4 | Render (cut + 9:16 + captions) | `captions.py`, `render.py` | **done** |
| 5 | Batch / end-to-end | `pipeline.py` | **todo** |

## Run commands

```
venv\Scripts\python.exe main.py check-env
venv\Scripts\python.exe main.py check-env --smoke-whisper
venv\Scripts\python.exe main.py download "<youtube-url>"
venv\Scripts\python.exe main.py download "path\to\local.mp4"
venv\Scripts\python.exe main.py download "<url-or-path>" --force
venv\Scripts\python.exe main.py transcribe <video_id> --preview 3
venv\Scripts\python.exe main.py transcribe <video_id> --force
venv\Scripts\python.exe main.py select <video_id> --top 3
venv\Scripts\python.exe main.py render <video_id> --clip 0
```

`select` needs a finished transcript. `--top N` overrides
`selection.max_clips` for that run; clips are printed **best score first**
(their `index` is rank order, not time order) with start/end, duration, score,
per-component breakdown, a short reason, and the clip's transcript text.

`clips.json` shape:

```json
{
  "selector": "heuristic",
  "settings": {"min_seconds": 20.0, "max_seconds": 60.0, "max_clips": 3},
  "reason": "",
  "clips": [
    {"index": 0, "start": 16.64, "end": 37.5, "score": 1.0876,
     "text": "...", "reason": "balanced",
     "components": {"speech_density": 0.70, "keywords": 0.0, "hook": 0.15,
                    "completeness": 0.6, "filler": -0.0, "silence": -0.37}}
  ]
}
```

`reason` is only filled when `clips` is empty (e.g. video shorter than
`min_seconds`, no speech, or no segment run fits the duration limits). The
command still exits 0 in that case — an empty result is a valid answer, not a
crash.

`render` runs **one** ffmpeg pass: cut, aspect conversion, caption burn, x264,
AAC, faststart. Output lands in `output/` as:

```
<slug-of-title>_<video_id>_clip<rank+1>_<crop_mode>.mp4
e.g. loop-test_db54e99fad_clip01_blur_pad.mp4
```

It always probes the result before reporting success and fails with a clear
message if the resolution, audio stream, or duration do not match. The final
summary prints the crop mode, caption event count, file size, render time, and
verified `1080x1920` / audio / duration.

`download` prints the `video_id` the next stage needs. `transcribe` prints a
summary plus the first N segments (number of segments, words, timestamps) and
logs throughput:

```
INFO  loading whisper model 'base' (int8, 6 threads)
INFO  model ready in 1.4s
INFO  transcribing audio.wav
INFO  transcribed 0:19 of audio in 3.0s (RTF 0.16, 6.3x realtime)
```

RTF = processing seconds / audio seconds. Below 1.0 is faster than realtime.

Planned (not built yet):

```
python main.py run <url-or-path> [--clips N]
```

## Module notes (for the next session)

- `config.py` — `load_config(path=None) -> Config`. Every tunable comes from
  `config.yaml`; relative paths resolve against the config file's folder.
  **Unknown keys are a hard error** (typo protection). `ensure_directories()`.
- `models.py` — `Word`, `Segment`, `Transcript` (+ `words_between`,
  `text_between`), `Clip`. All have `to_dict()` / `from_dict()` for caching.
  Timestamps are seconds (float).
- `ffmpeg.py` — `run_ffmpeg(args, desc=..., cwd=...)` (adds
  `-y -loglevel error`, raises `FFmpegError` with the last 25 stderr lines),
  `ffmpeg_output()`, `probe(path) -> MediaInfo(duration, width, height, fps,
  has_audio)`, `has_encoder()`, `has_filter()`, `escape_filter_path(path)`.
  `probe()` accepts audio-only files (width/height 0).
  **Note:** `-nostdin` is passed to ffmpeg only — ffprobe 9.0 rejects it.
- `state.py` — `State.for_source(cfg, url_or_path)` / `State.load(cfg, vid)`.
  `mark_running/mark_done/mark_failed`, `require(stage)`, `get()/set()`.
  Persists to `temp/<video_id>/state.json`.
- `download.py` — `SourceInfo` dataclass; `resolve_source(cfg, state, force=)`
  returns the cached result when the stage is `done` and the file still
  exists. URL path uses yt-dlp; local path is validated + probed (rejects
  missing files, non-videos, and videos with no audio track).
  `_translate_error()` maps yt-dlp errors to actionable messages
  (private / unavailable / age-restricted / bot check / geo / network).
- `audio.py` — `extract_audio(state, source, force=)` → `audio.wav`,
  16 kHz mono PCM s16le. Verifies duration against the video.
- `transcribe.py` — `transcribe(cfg, state, force=) -> Transcript`.
  Loads the model lazily (keeps `--help` fast), `word_timestamps=True`,
  `condition_on_previous_text=False` (stops hallucination loops on long
  videos), VAD per config. Writes `transcript.json` atomically and stores a
  cache signature (`model/compute/lang/vad`) in state: changing those settings
  re-transcribes automatically instead of silently reusing the old model's
  output. Model is freed with `del` + `gc.collect()` after use.
  `load_transcript(state) -> Transcript` reads the cache and raises a clear
  `StageError` telling you to run `transcribe` if it is missing.
- `selectors/base.py` — `Selector` protocol:
  `select(transcript, cfg) -> SelectionOutcome(clips, reason)`. The reason is
  how an empty result explains itself.
- `selectors/heuristic.py` — enumerates every window that starts and ends on a
  segment boundary and lasts `min_seconds..max_seconds`; scores each on six
  0..1 sub-scores, multiplies by `selection.weights`, and subtracts filler and
  silence penalties. Then greedy non-maximum suppression (best first, skip
  anything overlapping an already-kept window) until `max_clips`.
  Sub-scores: speech density (ideal 3.2 words/s), keyword hits (saturates at
  3), hook (question / opener word / digit / capitalised start), completeness
  (starts like a sentence, ends with punctuation), filler ratio, and dead air
  (gaps > 0.8 s, fully penalised at 4 s). Pure functions, no I/O.
- `selectors/__init__.py` — `load_selector(name)` registry (`heuristic` only
  for now); unknown names raise `ConfigError`. Add the LLM selector here and
  set `selection.selector` in config to swap it in.
- `captions.py` — pure caption builder, no ffmpeg. `build_events(transcript,
  clip, max_words)` returns clip-relative `CaptionEvent(start, end, text)`;
  `build_ass(...)` wraps them in an ASS document with
  `PlayResX/PlayResY = render.width/height`, bold white fill, black outline,
  bottom-center alignment (`Alignment=2`) and `captions.margin_v`.
  Chunks are 2–4 words, prefer punctuation boundaries, and borrow a word from
  the previous chunk instead of leaving an orphan. Events are held 0.15 s and
  trimmed at the next event so captions never overlap. `{`, `}`, and `\` are
  neutralised so whisper text cannot inject ASS markup.
- `render.py` — `render_clip(cfg, state, index) -> RenderResult`. Loads
  `clips.json`, writes `captions/clip_<index>.ass`, and runs one ffmpeg pass:
  `-ss`/`-t` cut → `scale/crop` (`center`) or blurred quarter-size background
  + centred foreground (`blur_pad`) → `fps` → `ass` → libx264 +
  `aac` + `+faststart`. It then `probe()`s the output and refuses success
  unless resolution, audio, and duration are correct. Output name is
  `<title-slug>_<video_id>_clip<rank+1>_<crop_mode>.mp4`.
- `tests/test_captions.py` — 8 stdlib tests for chunk sizes, punctuation,
  orphan merging, clip-relative timing, boundary trimming, style values, and
  markup escaping. Run with
  `venv\Scripts\python.exe -m unittest discover -s tests -v`.
- `tests/test_heuristic.py` — 10 stdlib `unittest` cases (scoring, boundaries,
  overlap, limit, empty/too-short/too-long edge cases). No whisper or ffmpeg.
  Run with `venv\Scripts\python.exe -m unittest discover -s tests -v`.
- `jsonio.py` — `write_json()` (atomic via `.tmp` + `os.replace`),
  `read_json()`; both raise `StageError` on failure.
- `formatting.py` — `format_duration()` ("2:41"), `format_size()` ("12.3 MB").
- `checks.py` — diagnostics for `check-env`; each check returns
  `(ok, detail, hint)` and never raises.
- `errors.py` — `ShortsBotError` base; CLI prints these without a traceback.
  Subclasses: `ConfigError`, `DependencyError`, `FFmpegError`,
  `DownloadError`, `TranscriptionError`, `RenderError`, `StageError`.

## Known issues / gotchas

1. **No JS runtime for yt-dlp.** Every download prints a warning that YouTube
   extraction without one is deprecated and some formats may be missing.
   Fix when it starts failing: install deno (`winget install DenoLand.Deno`).
2. **PyAV pinned to 16.1.0.** faster-whisper 1.2.1 calls
   `av.open(..., metadata_errors=...)`, removed in PyAV 17. Do not bump `av`
   past 16 until faster-whisper is updated.
3. **ffprobe rejects `-nostdin`** in ffmpeg 9.x ("Option not found"). Only
   ffmpeg gets that flag; handled in `ffmpeg._run`.
4. **First transcription downloads the model** (~145 MB for `base`,
   ~460 MB for `small`) into `%USERPROFILE%\.cache\huggingface`.
5. **HF symlink warning** on Windows is harmless (no Developer Mode).
6. **ffmpeg filter paths and apostrophes.** The filtergraph parser cannot
   escape a literal `'` inside a quoted option (tested: it truncates the path
   at the apostrophe), so **absolute caption paths are never put in `ass=`**.
   Caption files are ASCII-named (`captions/clip_00.ass`) and ffmpeg runs with
   `cwd=temp/<video_id>`; the filter only sees that relative path. Spaces and
   apostrophes anywhere in the project folder are therefore harmless.
   `ffmpeg.escape_filter_path()` exists for other filters but cannot make an
   absolute ASS path containing `'` safe.
7. **YouTube bot checks** — keep yt-dlp updated; `download.cookies_from_browser`
   (e.g. `"chrome"`) is the escape hatch and is already wired into yt-dlp opts.
8. **Measured speed** on this CPU (`base`, int8, 6 threads): model load
   ~1.0–1.4 s; 19 s of audio decoded in ~3.0 s = **RTF 0.16 (6.3x realtime)**.
   Expect `small` to be roughly 2–3x slower than `base`.
9. **Music-only videos produce no clips.** faster-whisper's VAD removes
   non-speech, so a song yields 0 segments and `select` reports "the
   transcript contains no speech segments". This is expected; a real
   narration/monologue is needed.
10. **`select` always recomputes and overwrites `clips.json`.** There is no
    selection cache on purpose: it is cheap, and config edits (`--top`,
    weights, keywords) should take effect immediately. Changing selection
    settings does not mark earlier stages stale.
11. **Clips are rank-ordered, not chronological.** `#0` is the highest score.
    Render stage 4 takes the `index` from `clips.json`, so do not assume
    `start` increases with `index`.
12. **Selection is only as good as segment timings.** Very long whisper
    segments (no pauses) can make the 20–60 s window impossible; the empty
    result names that case explicitly.
13. **Measured render speed** on this CPU for a 20.9 s clip, 1080x1920 @
    30 fps, `veryfast` / crf 21, AAC 128k:
    - `blur_pad`: 8.86 s, 7.17 s → avg **8.0 s** (~2.6x realtime)
    - `center`: 6.90 s, 7.43 s → avg **7.2 s** (~2.9x realtime)
    Center is ~10% faster because it upscales/crops a narrower region;
    blur_pad keeps the whole frame on a blurred background. Both CPU-only.
14. **Re-rendering overwrites the MP4** with the same name (same clip rank and
    crop mode). Changing `render.crop_mode` produces a second file with the
    other mode in its name. `captions/clip_<index>.ass` is regenerated each
    render, so caption config changes need no cache handling.

## Next action

Stage 4 is committed and awaiting the user's test. **Do not start Stage 5
until the user confirms.** Next up: `pipeline.py` — download → audio →
transcribe → select → render with per-clip failure isolation and a summary
table, plus `main.py run <url-or-path> [--clips N]`.
