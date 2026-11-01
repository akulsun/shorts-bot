# Shorts Bot

Turn long videos into vertical **1080x1920** YouTube Shorts with burned-in word-level captions. Fully local, CPU-friendly, and free with no paid APIs.

The pipeline executes four coordinated stages: **download → transcribe → select → render**.

---

## Features

- **Local & Private:** Everything runs locally on your CPU. No API keys, cloud accounts, or usage fees required.
- **Flexible Input:** Accepts YouTube URLs or local video files (`.mp4`, `.mov`, `.mkv`, etc.).
- **Word-Level Captions:** High-precision subtitle generation via `faster-whisper` (CTranslate2, int8 quantized).
- **Heuristic Clip Selection:** Automatically detects hook sentences, keyword density, speech rate, and natural speech pauses to score and pick the best 20–60 second clips.
- **One-Pass Vertical Rendering:** Cuts, aspect-converts (blurred padding or center crop), and burns formatted `.ass` subtitles in a single FFmpeg encode pass.
- **Smart Caching:** Work folders under `temp/<video_id>/` cache each stage; re-running reuses completed work without re-downloading or re-transcribing.
- **Desktop GUI & CLI:** Use either the command-line interface or the modern CustomTkinter desktop window.
- **Cooperative Cancellation & Safe Cleanup:** Cancel jobs mid-run cleanly without orphaned ffmpeg processes or corrupted files.

---

## Requirements

- **Operating System:** Windows 10/11 (tested), Linux, or macOS.
- **Python:** Python **3.10+** (tested up to Python 3.14).
- **FFmpeg & FFprobe:** Must be installed and available on system `PATH`.
  - Must include `libx264`, `aac`, and the `ass` subtitle filter.
  - **Windows Installation:**
    - Via winget: `winget install Gyan.FFmpeg`
    - Via Chocolatey: `choco install ffmpeg-full`
    - Or download a release from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) and add its `bin` folder to your system `PATH`.
- **JavaScript Runtime (for yt-dlp):** Downloading from YouTube requires a JavaScript runtime for yt-dlp (Deno recommended: `winget install DenoLand.Deno`).
- **Disk Space:** ~1–2 GB free disk space for temporary files and the whisper model.

---

## Installation

1. Clone this repository:
   ```bash
   git clone <repository-url>
   cd shorts-bot
   ```

2. Create and activate a virtual environment:
   - **Windows:**
     ```cmd
     python -m venv venv
     venv\Scripts\activate
     ```
   - **Linux / macOS:**
     ```bash
     python3 -m venv venv
     source venv/bin/activate
     ```

3. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Verify your environment:
   ```bash
   python main.py check-env
   ```
   To also download and test the Whisper model on a brief synthetic audio sample:
   ```bash
   python main.py check-env --smoke-whisper
   ```

---

## Usage

### 1. Desktop GUI

Launch the desktop interface:
```bash
python gui.py
```
*(or `python main.py gui`)*

- Enter a YouTube URL or click **Browse...** to pick a local video file.
- Adjust settings (number of clips, Whisper model, crop mode, min/max duration).
- Click **Start** to run the complete pipeline with live progress and logs.
- Click **Cancel** to safely stop execution at the next safe boundary.
- Once finished, view output clips in the **Results** panel with one-click buttons to play the video or reveal it in File Explorer.

### 2. One-Command CLI Pipeline

Run all stages (download, transcribe, select, render) in a single command:
```bash
python main.py run "<youtube-url-or-local-path>" --clips 3
```

Finished vertical videos are saved to `output/`.

#### Useful Flags:
- `--clean`: Deletes downloaded source media and intermediate WAV audio after a fully successful run (default is `--keep-temp`). Local files passed by the user are never deleted.
- `--force`: Ignores cached downloads or transcripts and re-runs from scratch.
- `-v` / `-vv`: Increases log verbosity for troubleshooting.

### 3. Individual Stage Commands

You can run individual stages independently using the printed 10-character `video_id`:
```bash
# Stage 1: Download or inspect local video and extract 16 kHz audio
python main.py download "https://youtube.com/watch?v=..."

# Stage 2: Transcribe audio to word-level timestamps (prints segment preview)
python main.py transcribe <video_id> --preview 3

# Stage 3: Pick top candidate clips based on heuristic score
python main.py select <video_id> --top 3

# Stage 4: Render a specific clip (rank index: 0 is the highest scored clip)
python main.py render <video_id> --clip 0
```

---

## Configuration

All tunables live in [`config.yaml`](file:///c:/Users/Akul/Desktop/shorts-bot/config.yaml):

| Section | Key | Default | Description |
|---|---|---|---|
| `transcription` | `model_size` | `base` | Whisper model (`tiny`, `base`, `small`, `medium`, `large-v3`). `base` is fast for testing; `small` is recommended for production quality. |
| `transcription` | `compute_type` | `int8` | Quantization type for CPU execution (`int8`, `float32`). |
| `transcription` | `cpu_threads` | `6` | Number of worker threads for transcription inference. |
| `transcription` | `vad_filter` | `true` | Silero Voice Activity Detector to filter out non-speech segments. |
| `selection` | `min_seconds` | `20` | Minimum duration for a selected clip. |
| `selection` | `max_seconds` | `60` | Maximum duration for a selected clip. |
| `selection` | `max_clips` | `3` | Maximum number of clips to select and produce. |
| `selection` | `selector` | `heuristic` | Clip selection algorithm (pluggable via `shortsbot/selectors/base.py`). |
| `selection` | `weights` | `{...}` | Relative weightings for speech density, hook sentences, keywords, filler words, and pauses. |
| `render` | `crop_mode` | `blur_pad` | `blur_pad` keeps the full original frame overlaid on a blurred background; `center` crops into the center. |
| `render` | `preset` | `veryfast` | x264 encoding preset (`ultrafast`, `veryfast`, `medium`, etc.). |
| `render` | `crf` | `21` | Constant Rate Factor (quality control; lower is higher quality, range 0–51). |
| `captions` | `enabled` | `true` | Whether to burn word-level subtitles into the final video. |
| `captions` | `font` | `Arial` | Subtitle font name. |
| `captions` | `font_size` | `72` | Subtitle font size. |
| `captions` | `max_words_per_line` | `4` | Number of words displayed per subtitle chunk (2–4 recommended). |

---

## Running Tests

Shorts Bot includes a comprehensive display-free unit and regression test suite covering captions formatting, heuristic scoring, pipeline progress, cooperative cancellation, safe file cleanup, and security input validation:

```bash
python -m unittest discover -s tests -v
```

---

## Project Structure

```
shorts-bot/
├── main.py                  # CLI entry point
├── gui.py                   # Desktop GUI entry point
├── config.yaml              # Runtime configuration
├── requirements.txt         # Pinned Python dependencies
├── LICENSE                  # MIT License
├── README.md                # Project documentation
├── docs/
│   └── DEV_NOTES.md         # Architecture, design decisions, and Windows quirks
├── shortsbot/
│   ├── __init__.py
│   ├── audio.py             # Audio extraction (16 kHz mono WAV)
│   ├── captions.py          # ASS subtitle format generator
│   ├── checks.py            # Environment diagnostics (check-env)
│   ├── cli.py               # Command-line parser & subcommands
│   ├── config.py            # Configuration loader and validator
│   ├── download.py          # yt-dlp wrapper & local file resolution
│   ├── errors.py            # Custom exception classes
│   ├── ffmpeg.py            # Process wrapper for ffmpeg & ffprobe
│   ├── formatting.py        # String format helpers (time, size)
│   ├── gui.py               # CustomTkinter GUI application
│   ├── gui_support.py       # Display-free GUI logic and helpers
│   ├── jsonio.py            # Atomic JSON read/write utilities
│   ├── logging_setup.py     # Logging configuration
│   ├── models.py            # Data structures (Word, Segment, Transcript, Clip)
│   ├── pipeline.py          # End-to-end execution orchestrator
│   ├── render.py            # FFmpeg vertical video render pipeline
│   ├── selection.py         # Selection stage coordinator
│   ├── state.py             # Working state persistence (temp/<id>/state.json)
│   ├── transcribe.py        # faster-whisper transcription wrapper
│   └── selectors/
│       ├── __init__.py      # Selector factory & registry
│       ├── base.py          # Selector protocol definition
│       └── heuristic.py     # Heuristic scoring selector
└── tests/                   # Automated unit & security test suite
    ├── test_captions.py
    ├── test_cleanup.py
    ├── test_ffmpeg_cancel.py
    ├── test_gui_support.py
    ├── test_heuristic.py
    ├── test_pipeline_progress.py
    └── test_security.py
```

---

## Known Limitations

- **CPU-Only Processing Speed:** Transcription and video rendering run on the CPU. A 10-minute video may take 1–3 minutes to process depending on processor cores and selected Whisper model size.
- **Heuristic Clip Selection:** The built-in selector relies on transcript density, hook patterns, and speech timing. It cannot evaluate visual aesthetics, camera angle switches, or non-verbal actions. (The pluggable `Selector` interface allows swapping in an LLM or multimodal selector).
- **No Face Tracking / Auto-Reframing:** Center crop extracts the fixed middle 9:16 region; if the subject moves away from the center, `blur_pad` mode is recommended to preserve the full frame.
- **Input Filenames Starting with Hyphens (Finding 1.2):** Paths beginning with a leading `-` passed as arguments could be treated as command-line flags by underlying CLI tools if not formatted as absolute or `./`-prefixed paths.
- **ASS Subtitle Font Header Escaping (Finding 2.1):** Font names specified in `captions.font` in `config.yaml` are inserted into the ASS header style definition without escaping commas or special characters; avoid commas or control characters in custom font names.
- **Audio Bitrate Configuration Syntax (Finding 2.2):** The `render.audio_bitrate` value from `config.yaml` is passed directly to FFmpeg's `-b:a` argument without format validation; use standard FFmpeg bitrate values (e.g., `128k`, `192k`).
- **Browser Cookie Extraction (Finding 4.2):** The `download.cookies_from_browser` setting is passed directly to `yt-dlp` without an internal browser name whitelist; specify supported browser names (such as `chrome`, `firefox`, `edge`).
- **Configurable Clip Bounds (Finding 5.1):** `selection.max_clips` and `selection.max_seconds` in `config.yaml` are validated for minimum positive values but do not enforce arbitrary hard upper ceilings; configure realistic values for your system resources.
- **Filesystem Path Display in GUI Errors (Finding 6.1):** When a file error occurs (such as a missing or inaccessible clip file), the GUI error popup displays the local filesystem path to assist with debugging.

---

## Responsible Use

Shorts Bot is an automated video editing tool created for content creators and researchers. Users must only download, transcribe, and edit content that they own, have licensed, or have explicit permission to use. This software does not grant any rights or licenses to copyrighted media, and users are solely responsible for complying with all applicable laws, copyright regulations, and platform terms of service.

---

## License

This project is licensed under the terms of the [MIT License](LICENSE).
