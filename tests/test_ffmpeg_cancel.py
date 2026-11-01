"""Exercise the cancellable ffmpeg subprocess path without running ffmpeg."""

from __future__ import annotations

import subprocess
import threading
import unittest
from unittest.mock import patch

from shortsbot import ffmpeg
from shortsbot.errors import CancelledError


class FakeProcess:
    def __init__(self, *, finish_on_first_poll: bool = False) -> None:
        self.returncode = 0 if finish_on_first_poll else None
        self.terminated = False
        self.killed = False
        self.finished = finish_on_first_poll

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.finished = True
        self.returncode = -15

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.finished = True
        self.returncode = -9

    def communicate(self, timeout=None):
        if not self.finished:
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)
        return "", ""


class CancellableFfmpegTests(unittest.TestCase):
    def test_cancel_terminates_the_child_and_raises(self):
        event = threading.Event()
        event.set()
        process = FakeProcess()

        with patch("shortsbot.ffmpeg.subprocess.Popen", return_value=process) as popen:
            with self.assertRaises(CancelledError):
                ffmpeg.run_ffmpeg(
                    ["-i", "input.mp4"],
                    desc="test render",
                    cancel_event=event,
                )

        popen.assert_called_once()
        self.assertTrue(process.terminated)
        self.assertFalse(process.killed)

    def test_normal_completion_still_returns_none(self):
        event = threading.Event()
        process = FakeProcess(finish_on_first_poll=True)

        with patch("shortsbot.ffmpeg.subprocess.Popen", return_value=process):
            result = ffmpeg.run_ffmpeg(
                ["-i", "input.mp4"],
                desc="test render",
                cancel_event=event,
            )

        self.assertIsNone(result)
        self.assertFalse(process.terminated)


if __name__ == "__main__":
    unittest.main()
