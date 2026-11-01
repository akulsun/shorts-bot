"""CustomTkinter desktop app for Shorts Bot (Stage 6 step A).

The UI is a thin layer: it validates form values, calls
`pipeline.run_pipeline()` in a worker thread, and renders queue messages.
No download/transcribe/render logic lives here.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any

import customtkinter as ctk

from . import pipeline
from .config import (
    CROP_MODES,
    MODEL_SIZES,
    Config,
    ensure_directories,
    load_config,
)
from .errors import CancelledError, ShortsBotError
from .gui_support import (
    GuiSettings,
    JobCancelled,
    JobFailed,
    JobSucceeded,
    LogEvent,
    QueueLogHandler,
    drain_queue,
    save_settings,
    validate_source,
)
from .logging_setup import setup_logging
from .pipeline import ProgressEvent

log = logging.getLogger("shortsbot.gui")

CANCEL_MESSAGE = "Cancelling... waiting for the current step to reach a safe point"
_LOG_FORMAT = "%(asctime)s  %(levelname)-5s  %(message)s"


class ShortsBotApp(ctk.CTk):
    def __init__(self, cfg: Config, *, poll_ms: int = 100) -> None:
        super().__init__()
        self.cfg = cfg
        self._poll_ms = poll_ms
        self._queue: queue.Queue[Any] = queue.Queue()
        self._handler = QueueLogHandler(self._queue)
        self._handler.setFormatter(logging.Formatter(_LOG_FORMAT, "%H:%M:%S"))
        logging.getLogger().addHandler(self._handler)

        self._worker: threading.Thread | None = None
        self._cancel_event: threading.Event | None = None
        self._job_active = False
        self._closing = False
        self._shut_down = False

        self.title("Shorts Bot")
        self.geometry("1000x780")
        self.minsize(820, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_ui()
        self._load_settings_into_form(cfg)
        self.after(self._poll_ms, self._poll_queue)
        log.info("GUI ready; config: %s", cfg.config_path)

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        bold = ctk.CTkFont(size=13, weight="bold")
        mono = ctk.CTkFont(family="Consolas", size=12)

        # Source --------------------------------------------------------- #
        source = ctk.CTkFrame(self)
        source.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        source.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(source, text="Source", width=56, anchor="w").grid(
            row=0, column=0, padx=(12, 6), pady=12
        )
        self.source_var = tk.StringVar()
        self.source_entry = ctk.CTkEntry(
            source,
            textvariable=self.source_var,
            placeholder_text=(
                "https://youtube.com/watch?v=... or C:\\videos\\input.mp4"
            ),
        )
        self.source_entry.grid(row=0, column=1, sticky="ew", pady=12)
        self.browse_button = ctk.CTkButton(
            source, text="Browse...", width=100, command=self._on_browse
        )
        self.browse_button.grid(row=0, column=2, padx=12, pady=12)

        # Settings ------------------------------------------------------- #
        settings = ctk.CTkFrame(self)
        settings.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 8))
        settings.grid_columnconfigure(6, weight=1)
        ctk.CTkLabel(settings, text="Settings", font=bold, anchor="w").grid(
            row=0, column=0, columnspan=7, sticky="w", padx=12, pady=(10, 2)
        )

        self.clips_var = tk.StringVar()
        self.model_var = tk.StringVar()
        self.crop_var = tk.StringVar()
        self.min_var = tk.StringVar()
        self.max_var = tk.StringVar()

        ctk.CTkLabel(settings, text="Clips", anchor="w").grid(
            row=1, column=0, sticky="w", padx=(12, 4), pady=4
        )
        self.clips_entry = ctk.CTkEntry(
            settings, width=70, textvariable=self.clips_var
        )
        self.clips_entry.grid(row=1, column=1, sticky="w", padx=(0, 14), pady=4)

        ctk.CTkLabel(settings, text="Whisper", anchor="w").grid(
            row=1, column=2, sticky="w", padx=(0, 4), pady=4
        )
        self.model_combo = ctk.CTkComboBox(
            settings,
            width=110,
            values=self._model_values(),
            variable=self.model_var,
            state="readonly",
        )
        self.model_combo.grid(
            row=1, column=3, sticky="w", padx=(0, 14), pady=4
        )

        ctk.CTkLabel(settings, text="Crop", anchor="w").grid(
            row=1, column=4, sticky="w", padx=(0, 4), pady=4
        )
        self.crop_combo = ctk.CTkComboBox(
            settings,
            width=120,
            values=list(CROP_MODES),
            variable=self.crop_var,
            state="readonly",
        )
        self.crop_combo.grid(
            row=1, column=5, sticky="w", padx=(0, 14), pady=4
        )
        self.save_button = ctk.CTkButton(
            settings, text="Save settings", width=130, command=self._on_save
        )
        self.save_button.grid(
            row=1, column=6, sticky="e", padx=12, pady=4
        )

        ctk.CTkLabel(settings, text="Min seconds", anchor="w").grid(
            row=2, column=0, sticky="w", padx=(12, 4), pady=(0, 10)
        )
        self.min_entry = ctk.CTkEntry(
            settings, width=70, textvariable=self.min_var
        )
        self.min_entry.grid(
            row=2, column=1, sticky="w", padx=(0, 14), pady=(0, 10)
        )
        ctk.CTkLabel(settings, text="Max seconds", anchor="w").grid(
            row=2, column=2, sticky="w", padx=(0, 4), pady=(0, 10)
        )
        self.max_entry = ctk.CTkEntry(
            settings, width=70, textvariable=self.max_var
        )
        self.max_entry.grid(
            row=2, column=3, sticky="w", padx=(0, 14), pady=(0, 10)
        )
        ctk.CTkLabel(
            settings,
            text="20-60 s is the usual clip range.",
            text_color=("gray40", "gray70"),
            anchor="w",
        ).grid(
            row=2,
            column=4,
            columnspan=2,
            sticky="w",
            padx=(0, 8),
            pady=(0, 10),
        )

        # Run controls --------------------------------------------------- #
        actions = ctk.CTkFrame(self)
        actions.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 8))
        actions.grid_columnconfigure(2, weight=1)
        self.start_button = ctk.CTkButton(
            actions, text="Start", width=110, command=self._on_start
        )
        self.start_button.grid(row=0, column=0, padx=(12, 6), pady=(12, 4))
        self.cancel_button = ctk.CTkButton(
            actions,
            text="Cancel",
            width=100,
            state="disabled",
            command=self._on_cancel,
        )
        self.cancel_button.grid(row=0, column=1, padx=(0, 12), pady=(12, 4))
        self.progress = ctk.CTkProgressBar(actions)
        self.progress.grid(
            row=0, column=2, sticky="ew", padx=(0, 12), pady=(12, 4)
        )
        self.progress.set(0)
        self.stage_label = ctk.CTkLabel(
            actions, text="Idle", anchor="w"
        )
        self.stage_label.grid(
            row=1, column=2, sticky="w", padx=(0, 12), pady=(0, 4)
        )
        self.error_label = ctk.CTkLabel(
            actions,
            text="",
            text_color="#ff6b6b",
            anchor="w",
            justify="left",
            wraplength=900,
        )
        self.error_label.grid(
            row=2,
            column=0,
            columnspan=3,
            sticky="ew",
            padx=12,
            pady=(0, 10),
        )

        # Log ------------------------------------------------------------ #
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=3, column=0, sticky="nsew", padx=14, pady=(0, 8))
        log_frame.grid_columnconfigure(0, weight=1)
        log_frame.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(log_frame, text="Log", font=bold, anchor="w").grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 2)
        )
        self.log_box = ctk.CTkTextbox(
            log_frame, wrap="word", font=mono
        )
        self.log_box.grid(
            row=1, column=0, sticky="nsew", padx=12, pady=(0, 12)
        )
        self.log_box.configure(state="disabled")

        # Results placeholder (full list arrives in step B) -------------- #
        results = ctk.CTkFrame(self)
        results.grid(row=4, column=0, sticky="ew", padx=14, pady=(0, 14))
        results.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(results, text="Results", font=bold, anchor="w").grid(
            row=0, column=0, sticky="w", padx=12, pady=(10, 2)
        )
        self.results_label = ctk.CTkLabel(
            results,
            text="Results will appear here after a run.",
            anchor="w",
        )
        self.results_label.grid(
            row=1, column=0, sticky="ew", padx=12, pady=(0, 10)
        )

    def _model_values(self) -> list[str]:
        values = list(MODEL_SIZES)
        current = self.cfg.transcription.model_size
        if current not in values:
            values.insert(0, current)
        return values

    # ------------------------------------------------------------------ #
    # Settings
    # ------------------------------------------------------------------ #

    def _load_settings_into_form(self, cfg: Config) -> None:
        settings = GuiSettings.from_config(cfg)
        self.clips_var.set(str(settings.max_clips))
        self.model_var.set(settings.model_size)
        self.crop_var.set(settings.crop_mode)
        self.min_var.set(f"{settings.min_seconds:g}")
        self.max_var.set(f"{settings.max_seconds:g}")
        values = self._model_values()
        self.model_combo.configure(values=values)

    def _read_settings(self) -> GuiSettings:
        return GuiSettings.from_form(
            max_clips=self.clips_var.get(),
            model_size=self.model_var.get(),
            crop_mode=self.crop_var.get(),
            min_seconds=self.min_var.get(),
            max_seconds=self.max_var.get(),
        )

    def _on_save(self) -> None:
        if self._job_active:
            return
        try:
            settings = self._read_settings()
            self.cfg = save_settings(settings, self.cfg)
        except Exception as exc:  # ConfigError, OSError, etc.
            log.warning("settings not saved: %s", exc)
            self._set_error(f"Settings not saved: {exc}")
            return

        self._set_error("")
        self._load_settings_into_form(self.cfg)
        log.info("settings saved to %s", self.cfg.config_path)

    # ------------------------------------------------------------------ #
    # Source / start / cancel
    # ------------------------------------------------------------------ #

    def _on_browse(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Choose a video",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self.source_var.set(path)
            self._set_error("")

    def _on_start(self) -> None:
        if self._job_active:
            return
        try:
            source = validate_source(self.source_var.get())
            settings = self._read_settings()
            cfg = settings.apply(self.cfg)
        except Exception as exc:  # ConfigError from validation
            self._set_error(str(exc))
            log.warning("cannot start: %s", exc)
            return

        self._set_error("")
        self._job_active = True
        self._cancel_event = threading.Event()
        self._set_running(True)
        self.progress.set(0)
        self.stage_label.configure(text="Starting...")
        self.results_label.configure(text="Running...")
        log.info("starting job: %s", source)
        log.info(
            "run settings: clips=%d, model=%s, crop=%s, range=%g-%gs",
            settings.max_clips,
            settings.model_size,
            settings.crop_mode,
            settings.min_seconds,
            settings.max_seconds,
        )

        self._worker = threading.Thread(
            target=self._run_job,
            args=(cfg, source, self._cancel_event),
            name="shortsbot-job",
            daemon=True,
        )
        self._worker.start()

    def _run_job(
        self,
        cfg: Config,
        source: str,
        cancel_event: threading.Event,
    ) -> None:
        """Worker thread: only pipeline calls and queue puts, no widgets."""
        try:
            result = pipeline.run_pipeline(
                cfg,
                source,
                progress=self._queue.put,
                cancel_event=cancel_event,
            )
        except CancelledError:
            log.warning("run cancelled")
            self._queue.put(JobCancelled())
        except ShortsBotError as exc:
            log.error("%s", exc)
            self._queue.put(JobFailed(str(exc)))
        except Exception as exc:  # noqa: BLE001 - keep the window alive
            log.exception("unexpected error in GUI job")
            self._queue.put(
                JobFailed(
                    f"Unexpected error: {exc}\n"
                    "See the log file for details."
                )
            )
        else:
            self._queue.put(JobSucceeded(result))

    def _on_cancel(self) -> None:
        if not self._job_active or self._cancel_event is None:
            return
        if self._cancel_event.is_set():
            return
        self._cancel_event.set()
        self.cancel_button.configure(state="disabled")
        self.stage_label.configure(text=CANCEL_MESSAGE)
        self._set_error(CANCEL_MESSAGE, "#f0b429")
        log.warning(CANCEL_MESSAGE)

    # ------------------------------------------------------------------ #
    # Queue / state
    # ------------------------------------------------------------------ #

    def _poll_queue(self) -> None:
        for event in drain_queue(self._queue):
            try:
                self._handle_event(event)
            except Exception:  # noqa: BLE001 - never stall the poll loop
                log.exception("GUI event handling failed")

        if self._closing:
            if self._job_active:
                self.after(self._poll_ms, self._poll_queue)
            else:
                self._shutdown()
            return
        self.after(self._poll_ms, self._poll_queue)

    def _handle_event(self, event: Any) -> None:
        if isinstance(event, LogEvent):
            self._append_log(event.text)
            return

        if isinstance(event, ProgressEvent):
            self.progress.set(event.fraction)
            self.stage_label.configure(text=event.message)
            return

        if isinstance(event, JobSucceeded):
            self._finish_job()
            self.progress.set(1.0)
            self.stage_label.configure(text="Done")
            self._set_error("")
            result = event.result
            selected = len(result.selection.outcome.clips)
            if selected == 0:
                reason = result.selection.outcome.reason
                self.results_label.configure(
                    text=(
                        "No clips were selected"
                        + (f": {reason}" if reason else ".")
                    )
                )
            else:
                self.results_label.configure(
                    text=(
                        f"Finished: {len(result.successes)} clip(s) rendered, "
                        f"{len(result.failures)} failed."
                    )
                )
            log.info(
                "finished: %d rendered, %d failed",
                len(result.successes),
                len(result.failures),
            )
            return

        if isinstance(event, JobFailed):
            self._finish_job()
            self.stage_label.configure(text="Failed")
            self._set_error(event.message)
            return

        if isinstance(event, JobCancelled):
            self._finish_job()
            self.stage_label.configure(text="Cancelled")
            self._set_error("Cancelled.", "#f0b429")

    def _finish_job(self) -> None:
        self._job_active = False
        self._worker = None
        self._cancel_event = None
        if not self._closing:
            self._set_running(False)

    def _set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.source_entry.configure(state=state)
        self.browse_button.configure(state=state)
        self.clips_entry.configure(state=state)
        self.model_combo.configure(state="disabled" if running else "readonly")
        self.crop_combo.configure(state="disabled" if running else "readonly")
        self.min_entry.configure(state=state)
        self.max_entry.configure(state=state)
        self.save_button.configure(state=state)
        self.start_button.configure(state="disabled" if running else "normal")
        self.cancel_button.configure(state="normal" if running else "disabled")

    def _set_error(self, text: str, color: str = "#ff6b6b") -> None:
        self.error_label.configure(text=text, text_color=color)

    def _append_log(self, text: str) -> None:
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text.rstrip() + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # Close
    # ------------------------------------------------------------------ #

    def _on_close(self) -> None:
        if self._shut_down:
            return
        if not self._job_active:
            self._shutdown()
            return
        if self._closing:
            return
        if not messagebox.askyesno(
            "Shorts Bot",
            "A job is running. Cancel it and close when it stops?",
            parent=self,
        ):
            return
        self._closing = True
        self._on_cancel()

    def _shutdown(self) -> None:
        if self._shut_down:
            return
        self._shut_down = True
        logging.getLogger().removeHandler(self._handler)
        self._handler.close()
        self.destroy()


def run_gui(cfg: Config) -> int:
    """Create and run the window for an already-loaded config."""
    ctk.set_appearance_mode("System")
    ctk.set_default_color_theme("blue")
    app = ShortsBotApp(cfg)
    app.mainloop()
    return 0


def main(config_path: str | Path | None = None) -> int:
    """Entry point for `python gui.py`, including startup error dialogs."""
    try:
        cfg = load_config(config_path)
    except ShortsBotError as exc:
        _show_startup_error(str(exc))
        return 2

    setup_logging(cfg.logging.level, cfg.logging.file)
    try:
        ensure_directories(cfg)
    except ShortsBotError as exc:
        _show_startup_error(str(exc))
        return 2
    return run_gui(cfg)


def _show_startup_error(message: str) -> None:
    try:
        root = ctk.CTk()
        root.withdraw()
        messagebox.showerror("Shorts Bot", message, parent=root)
        root.destroy()
    except Exception:  # noqa: BLE001 - last resort if Tk itself fails
        print(f"Shorts Bot: {message}", file=sys.stderr)
