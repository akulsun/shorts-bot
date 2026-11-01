# Shorts Bot

Turn a long video into vertical **1080x1920** YouTube Shorts with burned-in
word-level captions. Fully local, CPU-only, no paid APIs.

The pipeline is: **download → transcribe → select → render**.

## Requirements

- Windows (tested on Windows 10/11) or Linux/macOS with equivalent tools
- Python **3.10+** (tested with 3.14.8)
- `ffmpeg` and `ffprobe` on `PATH` (a build with `libx264`, `aac`, and the
  `ass` filter)
- ~1–2 GB free disk for the whisper model and temporary files
- No GPU required
- The desktop app uses `customtkinter==6.0.0`, already included in
  `requirements.txt`

## Setup

From the project folder:

```
python -m venv venv
venv\Scripts\python.exe -m pip install -r requirements.txt
venv\Scripts\python.exe main.py check-env
```

If `check-env` reports a missing component, install it and run the command
again. To also download and test the whisper model:

```
venv\Scripts\python.exe main.py check-env --smoke-whisper
```

The first transcription downloads the model (`base` ≈ 145 MB, `small` ≈ 460 MB)
into `%USERPROFILE%\.cache\huggingface`.

## Run everything in one command

```
venv\Scripts\python.exe main.py run "<youtube-url-or-local-path>" --clips 3
```

This runs all four stages and prints a progress line per stage plus a summary
of every output file and its duration. Finished MP4s go to `output/`.

Re-running the same source reuses the cached download, transcript, and
already-rendered clips, so an interrupted or partly failed run continues where
it stopped. If one clip fails to render, the error is logged and the remaining
clips still render; the command exits with a non-zero code.

By default temporary source video and audio are kept. Add `--clean` to delete
them after a **fully successful** run (default: `--keep-temp`):

```
venv\Scripts\python.exe main.py run "<url-or-path>" --clips 3 --clean
```

`--clean` only removes the downloaded copy inside `temp/<id>/` and the
extracted `audio.wav`. A local input file you passed in is never deleted.

## Run stages individually

`download` prints the `video_id` used by the later commands:

```
venv\Scripts\python.exe main.py download "<youtube-url>"
venv\Scripts\python.exe main.py download "path\to\local.mp4"
venv\Scripts\python.exe main.py transcribe <video_id> --preview 3
venv\Scripts\python.exe main.py select <video_id> --top 3
venv\Scripts\python.exe main.py render <video_id> --clip 0
```

Useful variants:

```
... download "<url-or-path>" --force      # re-download / re-extract audio
... transcribe <video_id> --force         # ignore the cached transcript
... transcribe <video_id> --preview 0     # skip the preview
... run "<url-or-path>" --clips 1         # one clip instead of config
... run "<url-or-path>" --keep-temp       # explicit default
```

## Desktop app

Start the window from the project folder:

```
venv\Scripts\python.exe gui.py
```

or through the CLI:

```
venv\Scripts\python.exe main.py gui
```

Paste a YouTube URL or choose a local file, adjust the five settings, and click
**Start**. The log panel mirrors the normal pipeline log, and the progress bar
covers download, transcription, selection, and rendering. Start and the
settings fields are disabled while a job runs.

**Save settings** writes Clips, Whisper model, Crop, Min seconds, and Max
seconds to `config.yaml`. Values are validated first; if anything is wrong, the
app shows the error and leaves `config.yaml` untouched. Unsaved form values
still apply to the next run started from the window.

**Cancel** asks the pipeline to stop at its next safe point and logs
`Cancelling... waiting for the current step to reach a safe point`. A render
stops within a couple of seconds; transcription can take up to one decoded
segment. Closing the window during a job asks for confirmation and waits for
the worker to stop.

This first GUI step shows a one-line result summary; the full per-clip results
list is next.

## Configuration

Edit `config.yaml`; all tunables live there and unknown keys fail loudly.

| Section | Keys that matter most |
|---|---|
| `transcription` | `model_size` (`base`, `small`, …), `compute_type`, `cpu_threads`, `language`, `vad_filter` |
| `selection` | `min_seconds` / `max_seconds` (20–60), `max_clips`, `keywords`, `weights`, `selector` |
| `render` | `crop_mode` (`blur_pad` keeps the whole frame; `center` crops), `width`, `height`, `fps`, `preset`, `crf`, `audio_bitrate` |
| `captions` | `enabled`, `font`, `font_size`, `max_words_per_line` (2–4 recommended), `margin_v`, `outline`, `bold` |
| `paths` | `temp` (working files), `output` (final MP4s) |

`selection.selector: heuristic` is the built-in picker. It is a swappable
interface (`shortsbot/selectors/base.py`), so an LLM selector can be added
later without changing the pipeline.

For faster iteration while testing, `transcription.model_size: base` is
currently set; switch to `small` for better accuracy once you are happy with
the speed.

## Troubleshooting

- **yt-dlp warns about no JavaScript runtime** — downloads still work; install
  deno if YouTube extraction starts failing.
- **"no clips were selected"** — the video has no speech, is shorter than
  `selection.min_seconds`, or has no segment run within the limits. Try a
  longer video or lower `min_seconds`.
- **Slow transcription** — CPU-only is expected. `base` is roughly 6x realtime
  on the reference Ryzen 4500U; `small` is about 2–3x slower.
- More detail and measured timings: `NOTES.md`.
