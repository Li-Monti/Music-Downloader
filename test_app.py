import argparse
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import app


class AppTests(unittest.TestCase):
    def test_song_and_playlist_links(self):
        for url in (
            "https://www.youtube.com/watch?v=example",
            "https://www.youtube.com/playlist?list=example",
            "https://music.youtube.com/playlist?list=example",
            "https://youtu.be/example?list=playlist",
        ):
            self.assertEqual(app.youtube_url(url), url)

    def test_invalid_links(self):
        for url in ("", "https://youtube.com.evil.test/watch?v=x", "https://youtube.com/", "https://example.com/playlist?list=x"):
            with self.assertRaises(argparse.ArgumentTypeError):
                app.youtube_url(url)

    def test_playlist_download_and_retry_options(self):
        url = "https://youtube.com/playlist?list=test&index=1"
        command = app.build_command(url, Path("/tmp/music"), "192")
        self.assertIn("--yes-playlist", command)
        self.assertIn("--no-abort-on-error", command)
        self.assertIn("--download-archive", command)
        self.assertEqual(command[-2:], ["--", url])
        self.assertIn("--no-playlist", app.build_command(url, Path("/tmp/music"), "192", True))

    def test_download_failure_is_not_reported_as_success(self):
        with patch("sys.argv", ["app.py", "https://youtu.be/test"]), \
             patch("app.shutil.which", return_value="/usr/bin/tool"), \
             patch("app.Path.mkdir"), \
             patch("app.find_spec", return_value=object()), \
             patch("app.subprocess.run", return_value=subprocess.CompletedProcess([], 1)), \
             patch("builtins.print"):
            self.assertEqual(app.main(), 1)

    def test_spotify_links(self):
        for kind in ("track", "playlist"):
            self.assertEqual(
                app.music_url(f"open.spotify.com/intl-es/{kind}/abc123?si=test"),
                f"https://open.spotify.com/{kind}/abc123",
            )
        for url in ("https://open.spotify.com/playlist/", "https://open.spotify.com/artist/abc", "https://open.spotify.com.evil.test/playlist/abc"):
            with self.assertRaises(argparse.ArgumentTypeError):
                app.music_url(url)
        self.assertEqual(app.music_url("https://youtu.be/example"), "https://youtu.be/example")

    def test_spotify_output_and_quality(self):
        command = app.build_spotify_command("https://open.spotify.com/playlist/abc", Path("/tmp/music with spaces"), "256", Path("/tmp/errors.txt"))
        self.assertEqual(command[:5], [app.sys.executable, "-m", "spotdl", "download", "https://open.spotify.com/playlist/abc"])
        self.assertEqual(command[command.index("--bitrate") + 1], "256k")
        self.assertIn("/tmp/music with spaces/Spotify/{list-name}/", command[command.index("--output") + 1])
        self.assertIn("--archive", command)
        self.assertEqual(command[command.index("--overwrite") + 1], "skip")

    def test_spotify_partial_failure_even_if_exit_code_is_zero(self):
        def fake_run(command, **kwargs):
            Path(command[command.index("--save-errors") + 1]).write_text("Canción no encontrada", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as folder, patch("app.subprocess.run", side_effect=fake_run), patch("builtins.print"):
            self.assertEqual(app.download_spotify("https://open.spotify.com/playlist/abc", Path(folder), "192"), 1)

    def test_spotify_success_has_no_stale_errors(self):
        with tempfile.TemporaryDirectory() as folder, patch("app.subprocess.run", return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(app.download_spotify("https://open.spotify.com/track/abc", Path(folder), "192"), 0)

    def test_main_routes_spotify(self):
        with patch("sys.argv", ["app.py", "https://open.spotify.com/playlist/abc", "--calidad", "320"]), \
             patch("app.find_spec", return_value=object()), \
             patch("app.shutil.which", return_value="/usr/bin/tool"), \
             patch("app.Path.mkdir"), patch("app.download_spotify", return_value=0) as download, \
             patch("app.subprocess.run") as youtube, patch("builtins.print"):
            self.assertEqual(app.main(), 0)
            self.assertEqual(download.call_args.args[0], "https://open.spotify.com/playlist/abc")
            self.assertEqual(download.call_args.args[2], "320")
            youtube.assert_not_called()

    def test_missing_spotify_dependency(self):
        with patch("sys.argv", ["app.py", "https://open.spotify.com/playlist/abc"]), \
             patch("app.find_spec", return_value=None), patch("app.subprocess.run") as run, patch("builtins.print"):
            self.assertEqual(app.main(), 1)
            run.assert_not_called()

    def test_spotify_skips_lyrics_but_keeps_covers_and_metadata(self):
        command = app.build_spotify_command("https://open.spotify.com/playlist/abc", Path("/tmp/music"), "192", Path("/tmp/errors"), parallel=6, fragments=2)
        self.assertIn("--lyrics", command)
        self.assertEqual(command[command.index("--threads") + 1], "6")
        self.assertNotIn("--skip-album-art", command)
        self.assertTrue(any("--concurrent-fragments 2" in part for part in command))
        with_lyrics = app.build_spotify_command("https://open.spotify.com/track/abc", Path("/tmp/music"), "192", Path("/tmp/errors"), lyrics=True)
        self.assertNotIn("--lyrics", with_lyrics)

    def test_youtube_starts_playlist_without_waiting_for_full_listing(self):
        command = app.build_command("https://youtube.com/playlist?list=abc", Path("/tmp/music"), "192", fragments=3)
        self.assertIn("--lazy-playlist", command)
        self.assertIn("--progress", command)
        self.assertEqual(command[command.index("--concurrent-fragments") + 1], "3")
        self.assertIn("--embed-metadata", command)

    def test_main_passes_performance_settings(self):
        with patch("sys.argv", ["app.py", "https://open.spotify.com/playlist/abc", "--paralelas", "2", "--fragmentos", "1", "--con-letras"]), \
             patch("app.find_spec", return_value=object()), patch("app.shutil.which", return_value="tool"), \
             patch("app.Path.mkdir"), patch("app.download_spotify", return_value=0) as download, patch("builtins.print"):
            self.assertEqual(app.main(), 0)
            self.assertEqual(download.call_args.args[3:], (2, 1, True))


if __name__ == "__main__":
    unittest.main()
