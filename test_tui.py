from pathlib import Path
import curses
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import tui


class Screen:
    def __init__(self, height=30, width=100):
        self.height, self.width = height, width
        self.rows = {}

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.rows.clear()

    def addstr(self, y, x, text, style=0):
        previous = self.rows.get(y, "").ljust(self.width)
        self.rows[y] = previous[:x] + text + previous[x + len(text):]

    def move(self, *args):
        pass

    def refresh(self):
        pass


class TerminalTests(unittest.TestCase):
    def make_app(self):
        with patch("tui.load_settings", return_value={"folder": "/tmp/music", "quality": "192"}):
            return tui.TerminalApp(Screen())

    def wait_job(self, job):
        deadline = time.monotonic() + 6
        lines = []
        while time.monotonic() < deadline:
            while not job.events.empty():
                lines.append(job.events.get_nowait())
            code = job.poll()
            if code is not None:
                return code, lines
            time.sleep(0.02)
        self.fail("El subproceso no terminó.")

    def test_settings_survive_restart_and_invalid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            tui.save_settings(Path(directory) / "música", "256", path)
            self.assertEqual(tui.load_settings(path)["quality"], "256")
            path.write_text("invalid", encoding="utf-8")
            self.assertEqual(tui.load_settings(path)["quality"], "192")

    def test_invalid_url_does_not_start_process(self):
        app = self.make_app()
        app.values["url"] = "https://example.com/video"
        with patch.object(app.job, "start") as start:
            app.start_download()
            start.assert_not_called()
        self.assertIn("Pegá un enlace", app.notice)

    def test_keyboard_edits_and_navigation(self):
        app = self.make_app()
        for character in "abc":
            app.handle_key(character)
        app.handle_key(curses.KEY_LEFT)
        app.handle_key(curses.KEY_BACKSPACE)
        self.assertEqual(app.values["url"], "ac")
        app.handle_key("\t")
        app.handle_key("\x15")
        for character in "/tmp/música":
            app.handle_key(character)
        self.assertEqual(app.values["folder"], "/tmp/música")

    def test_launch_uses_current_form_values(self):
        app = self.make_app()
        app.values["url"] = "youtu.be/test"
        app.quality = "320"
        with patch.object(app.job, "start") as start, patch("tui.save_settings"):
            app.start_download()
        start.assert_called_once_with("https://youtu.be/test", Path("/tmp/music"), "320", False, profile="equilibrado")

    def test_ui_renders_at_minimum_size_and_library_is_read_only(self):
        app = self.make_app()
        app.screen = Screen(24, 64)
        with patch("tui.curses.color_pair", return_value=0), patch("tui.curses.curs_set"):
            app.draw()
        self.assertIn("Descargar ↓", "\n".join(app.screen.rows.values()))
        with tempfile.TemporaryDirectory() as directory:
            song = Path(directory) / "Artista - Canción.mp3"
            song.write_bytes(b"example")
            app.values["folder"] = directory
            app.activate("library")
            self.assertEqual(app.files, [song])
            app.handle_key("\x1b")
            self.assertEqual(app.page, "download")
            self.assertEqual(song.read_bytes(), b"example")

    def test_external_output_and_failure_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            job = tui.DownloadJob()
            job.log_path = Path(directory) / "run.log"
            try:
                job.launch([sys.executable, "-u", "-c", "print('ERROR: video no disponible'); raise SystemExit(1)"])
                code, lines = self.wait_job(job)
                self.assertEqual(code, 1)
                self.assertIn("ERROR: video no disponible", lines)
                self.assertIn("video no disponible", job.log_path.read_text())
            finally:
                job.close()

    def test_cancel_stops_backend_and_its_child(self):
        with tempfile.TemporaryDirectory() as directory:
            child_pid = Path(directory) / "child.pid"
            child_script = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('child ready', flush=True); time.sleep(60)"
            parent_script = (
                "import subprocess,sys,time,pathlib; "
                f"p=subprocess.Popen([sys.executable,'-u','-c',{child_script!r}]); "
                f"pathlib.Path({str(child_pid)!r}).write_text(str(p.pid)); time.sleep(60)"
            )
            job = tui.DownloadJob()
            job.log_path = Path(directory) / "run.log"
            try:
                job.launch([sys.executable, "-u", "-c", parent_script])
                deadline = time.monotonic() + 3
                while not job.log_path.exists() or "child ready" not in job.log_path.read_text():
                    if time.monotonic() >= deadline:
                        self.fail("No arrancó el proceso hijo.")
                    time.sleep(0.02)
                pid = int(child_pid.read_text())
                job.cancel()
                self.wait_job(job)
                self.assertTrue(job.cancelled)
                stat = Path(f"/proc/{pid}/stat")
                self.assertTrue(not stat.exists() or stat.read_text().split()[2] == "Z")
            finally:
                job.close()

    def test_quit_requires_confirmation_while_downloading(self):
        app = self.make_app()
        app.job.process = object()
        app.quit()
        self.assertTrue(app.running)
        self.assertTrue(app.confirm_exit)
        app.handle_key("n")
        self.assertTrue(app.running)
        app.quit()
        app.handle_key("s")
        self.assertFalse(app.running)

    def test_unicode_width_respects_terminal_cells(self):
        self.assertEqual(tui.clipped("ab界c", 4), "ab界")
        self.assertEqual(tui.clipped("abc\x1b", 10), "abc")

    def test_layout_adapts_without_losing_buttons(self):
        for height, width in ((24, 64), (30, 100), (36, 168), (50, 200)):
            app = self.make_app()
            app.screen = Screen(height, width)
            with patch("tui.curses.color_pair", return_value=0), patch("tui.curses.curs_set"):
                app.draw()
            rendered = "\n".join(app.screen.rows.values())
            self.assertIn("ACTIVIDAD", rendered)
            self.assertIn("Descargar ↓", rendered)
            self.assertIn("╯", app.screen.rows[height - 3])
            self.assertEqual("TU DESCARGA" in rendered, min(width - 6, 118) >= 98)
            for row, left, right, key in app.hits:
                self.assertLess(row, height)
                self.assertLessEqual(right, width)
            app.activate("library")
            with patch("tui.curses.color_pair", return_value=0), patch("tui.curses.curs_set"):
                app.draw()
            app.activate("download-tab")
            self.assertEqual(app.page, "download")

    def test_speed_profile_is_saved_and_cannot_change_during_download(self):
        app = self.make_app()
        with patch("tui.save_settings") as save:
            app.handle_key(curses.KEY_F7)
            self.assertEqual(app.profile, "rapido")
            self.assertEqual(save.call_args.kwargs["profile"], "rapido")
        app.job.process = object()
        app.handle_key(curses.KEY_F7)
        self.assertEqual(app.profile, "rapido")

    def test_speed_profile_reaches_backend(self):
        with tempfile.TemporaryDirectory() as directory, patch("tui.BASE", Path(directory)):
            job = tui.DownloadJob()
            with patch.object(job, "launch") as launch:
                job.start("https://youtu.be/test", Path(directory), "192", profile="rapido")
            command = launch.call_args.args[0]
            self.assertEqual(command[command.index("--paralelas") + 1], "6")
            self.assertEqual(command[command.index("--fragmentos") + 1], "2")

    def test_idle_screen_is_not_redrawn_in_a_loop(self):
        app = self.make_app()
        app.screen.keypad = lambda *args: None
        app.screen.timeout = lambda *args: None
        events = iter([curses.error(), curses.error(), "\x11"])

        def next_key():
            key = next(events)
            if isinstance(key, Exception):
                raise key
            return key

        app.screen.get_wch = next_key
        with patch("tui.curses.raw"), patch("tui.curses.set_escdelay"), \
             patch("tui.curses.has_colors", return_value=False), patch("tui.curses.mousemask"), \
             patch.object(app, "draw") as draw:
            app.run()
        draw.assert_called_once()


if __name__ == "__main__":
    unittest.main()
