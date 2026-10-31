# Shorts Bot — Build Plan

Turns a YouTube URL or local video into 20–60 s vertical (1080x1920) clips with
burned-in captions. Fully local, CPU-only, no paid APIs.

## Stack

| Concern | Tool | Why |
|---|---|---|
| Transcription | `faster-whisper` (CTranslate2, int8) | word-level timestamps, CPU friendly |
| Video | `ffmpeg` / `ffprobe` | cut, crop, caption burn, encode in one pass |
| Download | `yt-dlp` | importable as a library, no shell parsing |
| Config | `PyYAML` | human editable |
| CLI | `argparse` | stdlib, no extra deps |

Everything else is stdlib (`subprocess`, `json`, `dataclasses`, `logging`).

## Folder structure

```
shorts-bot/
├── main.py                  # thin entrypoint -> shortsbot.cli
├── config.yaml              # all tunables, no hardcoded paths in code
├── requirements.txt
├── NOTES.md                 # living state doc for future sessions
├── docs/PLAN.md             # this file
├── shortsbot/
│   ├── __init__.py
│   ├── config.py            # load/validate config.yaml -> typed dataclasses
│   ├── models.py            # Word, Segment, Transcript, Clip
│   ├── state.py             # temp/<id>/state.json: resume + stage caching
│   ├── logging_setup.py     # console + file logging
│   ├── ffmpeg.py            # run_ffmpeg(), probe(), filter-path escaping
│   ├── cli.py               # argparse subcommands
│   ├── download.py          # yt-dlp / local file -> source path
│   ├── audio.py             # ffmpeg -> 16 kHz mono wav
│   ├── transcribe.py        # faster-whisper -> Transcript, JSON cache
│   ├── captions.py          # Transcript+Clip -> .ass subtitle file
│   ├── render.py            # cut + 9:16 + burn captions -> mp4
│   ├── pipeline.py          # run all stages end to end
│   └── selectors/
│       ├── __init__.py      # load_selector(name) factory — the swap point
│       ├── base.py          # Selector protocol
│       └── heuristic.py     # default scoring implementation
├── downloads/               # ignored
├── temp/                    # ignored
└── output/                  # ignored
```

## Work directory layout

One folder per source video, keyed by a short hash of the input, so every
stage can be re-run independently and cheaply:

```
temp/<video_id>/
├── state.json          # stage status + resolved paths
├── source.mp4          # downloaded (local files are referenced, not copied)
├── audio.wav           # 16 kHz mono
├── transcript.json     # cached so re-runs skip whisper
├── clips.json          # selected clips + scores + reasons
└── captions/clip_01.ass
output/<slug>_clip01.mp4
```

Deleting `temp/<video_id>/` forces a full re-run of that video only.

## Stages

| # | Stage | Module | Produces | Status |
|---|---|---|---|---|
| 0 | Foundation | `config.py`, `models.py`, `ffmpeg.py`, `state.py`, `cli.py`, `logging_setup.py` | importable package, `check-env` smoke test | todo |
| 1 | Download + audio | `download.py`, `audio.py` | `source.mp4`, `audio.wav` | todo |
| 2 | Transcribe | `transcribe.py` | `transcript.json` (word-level) | todo |
| 3 | Select clips | `selectors/` | `clips.json` | todo |
| 4 | Render | `captions.py`, `render.py` | `output/*.mp4` | todo |
| 5 | Batch / end-to-end | `pipeline.py` | summary + all clips | todo |

Stages 4 groups "cut + vertical + captions" into **one** stage on purpose: the
caption file is built by a pure function (`build_ass`) and then burned in the
*same* ffmpeg invocation as the crop. Three separate encodes of every clip
would roughly triple wall-clock time on a 4500U.

### Stage 0 — Foundation

- `config.py`: parse `config.yaml` into frozen dataclasses, validate ranges.
- `models.py`: `Word`, `Segment`, `Transcript`, `Clip` + helpers
  (`words_between`, `text_between`).
- `ffmpeg.py`: `run_ffmpeg(args, desc)` with exit-code capture and the last
  30 lines of stderr in the error message; `probe(path)` for duration/streams.
- `cli.py`: one subcommand per stage (`download`, `transcribe`, `select`,
  `render`, `run`, `check-env`).
- **Test:** `python main.py check-env` + a 5-second whisper smoke test on
  generated silence. This de-risks the Python 3.14 wheels *before* we build
  on top of them.

### Stage 1 — Download + audio

- `download.py`: YouTube URL -> `yt-dlp` (mp4, <=1080p, merged) ;
  local path -> validate exists + probe it. Returns a `SourceInfo`
  (`video_id`, `title`, `path`, `duration`).
- `audio.py`: `extract_audio()` -> `-vn -ac 1 -ar 16000 -c:a pcm_s16le`.
- Errors: missing yt-dlp, bot-check, private/deleted video, unsupported codec.
- **Test:** `python main.py download "<url>"` prints id, title, duration,
  paths. Same command on a local `.mp4`.

### Stage 2 — Transcribe

- `transcribe()`: `WhisperModel(size, device="cpu", compute_type="int8",
  cpu_threads=N)`, `word_timestamps=True`, `vad_filter=True`.
- Writes `transcript.json`; re-runs load the cache unless `--force`.
- Model is freed (`del` + `gc.collect()`) after use to keep RAM flat.
- **Test:** `python main.py transcribe <id> --preview 3` prints segments with
  word counts and timings.

### Stage 3 — Select clips

- `Selector` protocol: `select(transcript, cfg) -> list[Clip]`.
- `load_selector(name)` factory reads `selection.selector` from config.
  Adding an LLM later = one new file + one registry entry, no pipeline edits.
- `HeuristicSelector`:
  1. candidate windows start and end on transcript segment boundaries, within
     `[min_seconds, max_seconds]`;
  2. score = speech density (words/s, peak around 2.5–4.5) + keyword hits +
     first-sentence hook (question / number / "mistake", "secret", "never",
     "how to") + completeness (clean start, terminal punctuation) − filler
     ratio ("um", "uh", "like", "you know") − long silence penalty;
  3. weights live in `config.yaml`;
  4. greedy non-max suppression so clips don't overlap.
- **Test:** `python main.py select <id> --top 3` — a scored table with the
  first line of text. No rendering, so iteration is seconds.

### Stage 4 — Render

- `captions.py`: `build_ass(transcript, clip, cfg) -> str`. 3–4 words per
  line, breaks on word timestamps, bold white text, black outline, positioned
  in the lower third (above the YouTube UI overlay).
- `render.py`: one ffmpeg pass —
  `-ss`/`-t` seek → `scale=...increase,crop=1080:1920` (or blurred fill) →
  `ass=captions.ass` → `libx264 -preset veryfast -crf 21 -pix_fmt yuv420p`
  → `aac 128k` → `+faststart`.
- Windows gotcha handled centrally: `ass=` needs `C\:/path/x.ass` with
  forward slashes and an escaped colon.
- **Test:** `python main.py render <id> --clip 0` then play the MP4.

### Stage 5 — Batch

- `pipeline.py`: download → audio → transcribe → select → render, per-clip
  failure isolation (one bad clip doesn't kill the batch), summary table.
- **Test:** `python main.py run "<url>" --clips 3`.

## Config sketch

```yaml
paths:   { downloads: downloads, temp: temp, output: output }
download: { max_height: 1080, cookies_from_browser: null }
transcription: { model_size: small, compute_type: int8, cpu_threads: 6,
                 language: null, vad_filter: true }
selection: { selector: heuristic, min_seconds: 20, max_seconds: 60,
             max_clips: 3, weights: { ... }, keywords: [ ... ] }
render:  { width: 1080, height: 1920, crop_mode: center,
           preset: veryfast, crf: 21 }
captions: { enabled: true, font: Arial, font_size: 72,
            max_words_per_line: 4, margin_v: 380 }
```

## Risks / known issues

1. **Python 3.14 wheels** — `ctranslate2 4.8.4` and `onnxruntime 1.31.1` ship
   `cp314` win_amd64 wheels, so it should install. Validated in Stage 0;
   fallback is installing Python 3.12 and recreating the venv.
2. **Windows `ass=` escaping** — the classic cause of "Unable to open
   subtitle" errors; handled in one helper, not per call site.
3. **YouTube bot checks / 403** — `yt-dlp` needs regular updates; a
   `cookies_from_browser` config key exists for age-restricted cases.
4. **8 GB RAM** — strictly sequential: one clip at a time, whisper model
   freed after transcription, no parallel ffmpeg jobs.
5. **CPU speed** — a 1 h video at `small`/int8 is roughly 15–40 min of
   transcription on a 4500U. That's expected, not a bug.
6. **Center-crop** can cut off a speaker who isn't centered; `blur_pad` is
   available as a config alternative. Face tracking is out of scope.
7. **First transcribe needs internet** to fetch the ~460 MB model once.

## Explicitly out of scope (for now)

Face tracking, auto-reframing, music/B-roll, thumbnail generation, upload to
YouTube, GUI. Ask before adding any of these.
