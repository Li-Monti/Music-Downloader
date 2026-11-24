#!/usr/bin/env python3
"""Interfaz de terminal para la app MP3. No necesita paquetes adicionales."""

import argparse
from collections import deque
import curses
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from functools import lru_cache

from app import BASE, is_spotify, music_url


SETTINGS = BASE / ".preferencias.json"
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
QUALITIES = ("128", "192", "256", "320")
PROFILES = {
    "tranquilo": ("Tranquilo", 2, 1),
    "equilibrado": ("Equilibrado", 4, 2),
    "rapido": ("Rápido", 6, 2),
}
# Colores xterm de 256 entradas: no dependen de la paleta del usuario.
PALETTE = {
    1: (147, 234), 2: (245, 234), 3: (252, 235), 4: (255, 237),
    5: (215, 234), 6: (252, 234), 7: (252, 235), 8: (239, 234),
    9: (234, 147), 10: (157, 237), 11: (157, 235), 12: (210, 235),
    13: (252, 236), 14: (147, 235), 15: (239, 235), 16: (147, 234),
    17: (245, 235), 18: (245, 237),
}


@lru_cache(maxsize=4096)
def char_width(char):
    return 0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in "WF" else 1


def cell_width(text):
    return sum(char_width(c) for c in text)


def clipped(text, width):
    result = []
    used = 0
    for char in str(text):
        # Las salidas externas no deben introducir controles en la pantalla.
        if unicodedata.category(char).startswith("C"):
            continue
        size = char_width(char)
        if used + size > max(0, width):
            break
        result.append(char)
        used += size
    return "".join(result)


def load_settings(path=SETTINGS):
    defaults = {"folder": str(BASE / "musica"), "quality": "192", "profile": "equilibrado"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            if isinstance(data.get("folder"), str) and data["folder"].strip():
                defaults["folder"] = data["folder"]
            if data.get("quality") in QUALITIES:
                defaults["quality"] = data["quality"]
            if data.get("profile") in PROFILES:
                defaults["profile"] = data["profile"]
    except (OSError, ValueError):
        pass
    return defaults


def save_settings(folder, quality, path=SETTINGS, profile="equilibrado"):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"folder": str(folder), "quality": quality, "profile": profile}, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class DownloadJob:
    """Ejecuta el backend sin bloquear el teclado ni mezclar su salida con curses."""

    def __init__(self):
        self.process = None
        self.reader = None
        self.events = queue.Queue(maxsize=1000)
        self.started = 0
        self.cancelled = False
        self.kill_at = None
        self.log_path = None

    @property
    def active(self):
        return self.process is not None

    def start(self, url, folder, quality, only_song=False, profile="equilibrado"):
        if self.active:
            raise RuntimeError("Ya hay una descarga en curso.")
        folder.mkdir(parents=True, exist_ok=True)
        logs = BASE / ".logs"
        logs.mkdir(exist_ok=True)
        self.log_path = logs / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".log")
        command = [sys.executable, "-u", str(BASE / "app.py"), url, "-o", str(folder), "--calidad", quality]
        _, parallel, fragments = PROFILES[profile]
        command.extend(["--paralelas", str(parallel), "--fragmentos", str(fragments)])
        if only_song:
            command.append("--solo-cancion")
        self.launch(command)

    def launch(self, command):
        self.events = queue.Queue(maxsize=1000)
        self.cancelled = False
        self.kill_at = None
        self.started = time.monotonic()
        environment = dict(os.environ, PYTHONUNBUFFERED="1", NO_COLOR="1", TERM="dumb")
        self.process = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            env=environment, start_new_session=True,
        )
        self.reader = threading.Thread(target=self._read, args=(self.process.stdout,), daemon=True)
        self.reader.start()

    def _read(self, stream):
        try:
            with stream, self.log_path.open("w", encoding="utf-8", buffering=32768) as log:
                last_flush = 0
                for line in stream:
                    clean = ANSI.sub("", line).rstrip()
                    log.write(clean + "\n")
                    now = time.monotonic()
                    if now - last_flush >= 0.25 or "error" in clean.lower():
                        log.flush()
                        last_flush = now
                    try:
                        self.events.put_nowait(clean)
                    except queue.Full:
                        # El registro en disco siempre contiene la salida completa.
                        pass
        except OSError as exc:
            try:
                self.events.put_nowait(f"No se pudo guardar el registro: {exc}")
            except queue.Full:
                pass

    def _signal(self, sig):
        if self.process:
            try:
                os.killpg(self.process.pid, sig)
            except ProcessLookupError:
                pass

    def cancel(self):
        if self.active and not self.cancelled:
            self.cancelled = True
            self._signal(signal.SIGTERM)
            self.kill_at = time.monotonic() + 2

    def poll(self):
        if not self.active:
            return None
        if self.kill_at is not None and time.monotonic() >= self.kill_at:
            self._signal(signal.SIGKILL)
            self.kill_at = None
        code = self.process.poll()
        if code is not None and not self.reader.is_alive() and self.events.empty():
            self.process.wait()
            self.process = None
            self.kill_at = None
            return code
        return None

    def close(self):
        if self.active:
            self.cancel()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            # Incluye los subprocesos de yt-dlp y FFmpeg, incluso si el padre salió.
            self._signal(signal.SIGKILL)
            self.process.wait(timeout=2)
            self.reader.join(timeout=2)
            self.process = None


class TerminalApp:
    FIELDS = ("url", "folder", "quality", "single", "download", "cancel", "library", "quit")

    def __init__(self, screen):
        self.screen = screen
        settings = load_settings()
        self.values = {"url": "", "folder": settings["folder"]}
        self.cursors = {key: len(value) for key, value in self.values.items()}
        self.quality = settings["quality"]
        self.profile = settings.get("profile", "equilibrado")
        self.single = False
        self.focus = 0
        self.page = "download"
        self.notice = "Pegá un enlace para empezar. Tab cambia de campo."
        self.status = "LISTO PARA DESCARGAR"
        self.progress = ""
        self.lines = deque(maxlen=500)
        self.job = DownloadJob()
        self.files = []
        self.library_index = 0
        self.confirm_exit = False
        self.running = True
        self.hits = []

    def write(self, y, x, text, style=0, width=None):
        height, columns = self.screen.getmaxyx()
        if y < 0 or x < 0 or y >= height or x >= columns:
            return
        text = clipped(text, min(columns - x - 1, width if width is not None else columns))
        try:
            self.screen.addstr(y, x, text, style)
        except curses.error:
            pass

    def box(self, y, x, height, width, title="", focused=False):
        border = curses.color_pair(14 if focused else 15)
        for row in range(y, y + height):
            self.write(row, x, " " * width, curses.color_pair(7))
        self.write(y, x, "╭" + "─" * (width - 2) + "╮", border)
        self.write(y + height - 1, x, "╰" + "─" * (width - 2) + "╯", border)
        for row in range(y + 1, y + height - 1):
            self.write(row, x, "│", border)
            self.write(row, x + width - 1, "│", border)
        if title:
            self.write(y, x + 2, " " + title + " ", curses.color_pair(14 if focused else 17), width - 5)

    def field(self, y, x, width, label, key):
        focused = self.FIELDS[self.focus] == key
        self.box(y, x, 3, width, label, focused)
        value = self.values[key]
        cursor = self.cursors[key]
        available = width - 6
        left, used = cursor, 0
        while left > 0 and used + char_width(value[left - 1]) < available:
            left -= 1
            used += char_width(value[left])
        text = clipped(value[left:], available)
        style = curses.color_pair(4 if focused else 13)
        self.write(y + 1, x + 1, " " * (width - 2), style)
        placeholder = "Pegá el enlace acá…" if key == "url" else "Elegí dónde guardar tu música"
        self.write(y + 1, x + 3, text or placeholder, style if value else curses.color_pair(18 if focused else 13), available)
        self.hits.append((y + 1, x + 1, x + width - 1, key))
        if focused and not self.confirm_exit:
            self.cursor_position = (y + 1, x + 3 + cell_width(value[left:cursor]))

    def button(self, y, x, key, label, primary=False, disabled=False):
        focused = self.FIELDS[self.focus] == key
        text = ("› " if focused else "  ") + label + "  "
        pair = 17 if disabled else 9 if primary else 4 if focused else 7
        self.write(y, x, text, curses.color_pair(pair) | (curses.A_BOLD if primary else 0))
        self.hits.append((y, x, x + cell_width(text), key))
        return x + cell_width(text) + 1

    def draw(self):
        self.screen.erase()
        self.hits = []
        self.cursor_position = None
        height, width = self.screen.getmaxyx()
        if height < 24 or width < 64:
            self.write(1, 2, "MÚSICA / MP3", curses.color_pair(1) | curses.A_BOLD)
            self.write(4, 2, "Ampliá la terminal a 64 columnas y 24 filas.")
            self.write(6, 2, "F10 o Ctrl+Q para salir.")
            self.screen.refresh()
            return
        self.content_width = min(width - 6, 118)
        self.left = (width - self.content_width) // 2
        compact = height < 30
        self.top = 5 if compact else 6
        self.write(1, self.left, "♫  MÚSICA", curses.color_pair(1) | curses.A_BOLD)
        self.write(1, self.left + 13, "/ MP3", curses.color_pair(2))
        self.write(1, self.left + self.content_width - 21, " YOUTUBE  +  SPOTIFY ", curses.color_pair(10))
        if not compact:
            self.write(2, self.left, "Tu música, a un enlace de distancia.", curses.color_pair(2))
        nav_y = 3 if compact else 4
        active = self.page == "download"
        self.write(nav_y, self.left + 1, ("● " if active else "  ") + "Descargar", curses.color_pair(1 if active else 2) | curses.A_BOLD)
        self.write(nav_y, self.left + 20, ("● " if not active else "  ") + "Biblioteca", curses.color_pair(2 if active else 1) | curses.A_BOLD)
        profile_text = "F7 · " + PROFILES[self.profile][0]
        self.write(nav_y, self.left + self.content_width - len(profile_text) - 1, profile_text, curses.color_pair(2))
        self.hits.extend([(nav_y, self.left, self.left + 17, "download-tab"), (nav_y, self.left + 19, self.left + 38, "library")])
        self.hits.append((nav_y, self.left + self.content_width - len(profile_text) - 1, self.left + self.content_width, "profile"))
        if self.page == "library":
            self.draw_library(height, width)
        else:
            self.draw_download(height, width)
        self.write(height - 2, self.left + 1, self.notice, curses.color_pair(5) if self.confirm_exit else curses.color_pair(2), self.content_width - 2)
        shortcuts = "Tab Moverse   F5 Descargar   F6 Biblioteca   F7 Velocidad   Esc Volver   F10 Salir"
        if width < 85:
            shortcuts = "F5 Descargar · F6 Biblioteca · F7 Velocidad · F10 Salir"
        self.write(height - 1, self.left + 1, shortcuts, curses.color_pair(2), self.content_width - 2)
        if self.confirm_exit:
            modal_w = min(56, self.content_width - 2)
            mx, my = (width - modal_w) // 2, (height - 7) // 2
            self.box(my, mx, 7, modal_w, " DESCARGA EN CURSO ", True)
            self.write(my + 2, mx + 3, "¿Querés cancelar la descarga y salir?", curses.color_pair(3), modal_w - 6)
            self.write(my + 4, mx + 3, " S  Cancelar y salir ", curses.color_pair(9))
            self.write(my + 4, mx + 26, " N  Continuar ", curses.color_pair(3))
        try:
            curses.curs_set(1 if self.cursor_position else 0)
            if self.cursor_position:
                self.screen.move(*self.cursor_position)
        except curses.error:
            pass
        self.screen.refresh()

    def draw_download(self, height, width):
        compact = height < 30
        top, left, total = self.top, self.left, self.content_width
        form_height = 14 if compact else 18
        sidebar = total >= 98
        form_width = total - 32 if sidebar else total
        self.box(top, left, form_height, form_width, " NUEVA DESCARGA ")
        field_x, field_w = left + 2, form_width - 4
        self.field(top + (1 if compact else 2), field_x, field_w, " Enlace de YouTube o Spotify ", "url")
        self.field(top + (4 if compact else 6), field_x, field_w, " Guardar en ", "folder")
        quality_y = top + (7 if compact else 10)
        self.write(quality_y, left + 3, "Calidad del audio", curses.color_pair(17))
        for index, quality in enumerate(QUALITIES):
            x = left + 3 + index * 12
            chosen = self.quality == quality
            group_focus = self.FIELDS[self.focus] == "quality"
            style = curses.color_pair(9 if chosen and group_focus else 10 if chosen else 17)
            text = ("●" if chosen else " ") + f" {quality} kbps "
            self.write(quality_y + 1, x, text, style)
            self.hits.append((quality_y + 1, x, x + 11, "quality:" + quality))
        single_y = top + (10 if compact else 13)
        style = curses.color_pair(14 if self.FIELDS[self.focus] == "single" else 17)
        self.write(single_y, left + 3, ("[✓]" if self.single else "[ ]") + " Solo este video · YouTube", style)
        self.hits.append((single_y, left + 3, left + form_width - 2, "single"))
        button_y = top + form_height - 3
        x = self.button(button_y, left + 3, "download", "Descargar ↓", primary=not self.job.active, disabled=self.job.active)
        x = self.button(button_y, x, "cancel", "Cancelar", disabled=not self.job.active)
        x = self.button(button_y, x, "library", "Biblioteca")
        self.button(button_y, x, "quit", "Salir")
        if sidebar:
            self.draw_summary(top, left + form_width + 2, form_height, 30)
        activity_top = top + form_height + (0 if compact else 1)
        self.draw_activity(activity_top, left, max(3, height - activity_top - 2), total)

    def draw_summary(self, top, left, height, width):
        self.box(top, left, height, width, " TU DESCARGA ")
        self.write(top + 2, left + 3, "▂ ▅ ▃ █ ▆ ▂ ▄ ▇ ▃ ▅", curses.color_pair(14))
        source = "Spotify" if "spotify.com/" in self.values["url"] else "YouTube" if "youtu" in self.values["url"] else "YouTube o Spotify"
        for row, label, value in ((5, "FORMATO", "MP3"), (8, "CALIDAD", self.quality + " kbps"), (11, "ORIGEN", source)):
            if row + 2 < height:
                self.write(top + row, left + 3, label, curses.color_pair(17))
                self.write(top + row + 1, left + 3, value, curses.color_pair(3) | curses.A_BOLD, width - 6)
        if height >= 18:
            self.write(top + 15, left + 3, "Velocidad · " + PROFILES[self.profile][0], curses.color_pair(17))

    def draw_activity(self, top, left, height, width):
        self.box(top, left, height, width, " ACTIVIDAD ")
        badge = "EN CURSO" if self.job.active else "EN ESPERA" if not self.job.log_path else self.status
        badge = clipped(badge, min(25, width - 20))
        self.write(top, left + width - cell_width(badge) - 4, " " + badge + " ", curses.color_pair(12 if "ERRORES" in badge else 11))
        if height < 5:
            self.write(top + 1, left + 3, self.progress or self.status, curses.color_pair(17), width - 6)
            return
        if not self.job.active and not self.lines:
            message = "Tu próxima canción empieza acá."
            x = left + max(3, (width - cell_width(message)) // 2)
            row = top + max(1, (height - 2) // 2)
            self.write(row, x, message, curses.color_pair(3) | curses.A_BOLD)
            if row + 1 < top + height - 1:
                hint = "Pegá un enlace y elegí Descargar."
                self.write(row + 1, left + max(3, (width - cell_width(hint)) // 2), hint, curses.color_pair(17))
            return
        status = self.status
        if self.job.active:
            seconds = int(time.monotonic() - self.job.started)
            status = "◌  Procesando tu música" + f"   ·   {seconds // 60:02d}:{seconds % 60:02d}"
        self.write(top + 1, left + 3, status, curses.color_pair(3), width - 6)
        log_top = top + 2
        if self.job.active:
            match = re.search(r"(\d+(?:\.\d+)?)%", self.progress)
            if match:
                percent = min(100, max(0, float(match.group(1))))
                bar_width = min(40, width - 18)
                filled = round(bar_width * percent / 100)
                self.write(top + 2, left + 3, "━" * filled, curses.color_pair(14))
                self.write(top + 2, left + 3 + filled, "─" * (bar_width - filled), curses.color_pair(15))
                self.write(top + 2, left + 5 + bar_width, f"{percent:.0f}%", curses.color_pair(3))
                self.write(top + 3, left + 3, "Audio actual · " + self.progress, curses.color_pair(17), width - 6)
                log_top = top + 4
            else:
                frame = "◐◓◑◒"[int(time.monotonic() * 4) % 4]
                self.write(top + 2, left + 3, frame + " Buscando y preparando el audio…", curses.color_pair(17), width - 6)
                log_top = top + 3
        capacity = max(0, top + height - 1 - log_top)
        for index, line in enumerate(list(self.lines)[-capacity:] if capacity else []):
            self.write(log_top + index, left + 3, line, curses.color_pair(12 if "error" in line.lower() else 17), width - 6)

    def refresh_library(self):
        folder = Path(self.values["folder"]).expanduser()
        try:
            self.files = sorted(folder.rglob("*.mp3"), key=lambda item: str(item).casefold()) if folder.is_dir() else []
            self.library_index = min(self.library_index, max(0, len(self.files) - 1))
            self.notice = "↑/↓ recorrer · R actualizar · Esc volver"
        except OSError as exc:
            self.notice = f"No se pudo leer la carpeta: {exc}"

    def draw_library(self, height, width):
        top, left, panel_width = self.top, self.left, self.content_width
        panel_height = height - top - 3
        self.box(top, left, panel_height, panel_width, f" TU BIBLIOTECA · {len(self.files)} MP3 ")
        self.write(top + 2, left + 3, str(Path(self.values["folder"]).expanduser()), curses.color_pair(17), panel_width - 6)
        if not self.files:
            row = top + panel_height // 2
            self.write(row, left + 4, "Tu biblioteca está por empezar.", curses.color_pair(3) | curses.A_BOLD)
            self.write(row + 2, left + 4, "Volvé a Descargar y pegá tu primer enlace.", curses.color_pair(17), panel_width - 8)
            return
        self.write(top + 4, left + 3, "     CANCIÓN / ARTISTA", curses.color_pair(17))
        capacity = panel_height - 9
        start = max(0, self.library_index - capacity + 1)
        folder = Path(self.values["folder"]).expanduser()
        for index, path in enumerate(self.files[start:start + capacity], start=start):
            row = top + 5 + index - start
            selected = index == self.library_index
            style = curses.color_pair(4 if selected else 7)
            self.write(row, left + 2, " " * (panel_width - 4), style)
            self.write(row, left + 3, ("›" if selected else " ") + f" {index + 1:>3}  " + path.stem, style, panel_width - 6)
        self.write(top + panel_height - 3, left + 3, str(self.files[self.library_index].relative_to(folder)), curses.color_pair(17), panel_width - 6)
        self.write(top + panel_height - 2, left + 3, f"{self.library_index + 1} de {len(self.files)}  ·  ↑/↓ recorrer  ·  R actualizar", curses.color_pair(14))

    def start_download(self):
        if self.job.active:
            self.notice = "Ya hay una descarga en curso. Podés cancelarla con Esc."
            return
        try:
            url = music_url(self.values["url"])
            if not self.values["folder"].strip():
                raise ValueError("Elegí una carpeta de destino.")
            if self.single and is_spotify(url) and "/playlist/" in url:
                raise ValueError("Para Spotify, desmarcá 'Solo el video' o pegá el enlace de una canción.")
            folder = Path(self.values["folder"]).expanduser().resolve()
            self.job.start(url, folder, self.quality, self.single, profile=self.profile)
            self.lines.clear()
            self.progress = ""
            self.status = "DESCARGANDO"
            self.notice = "Esc cancela la descarga. Los temas completados se conservan."
            self.focus = self.FIELDS.index("cancel")
            try:
                save_settings(folder, self.quality, profile=self.profile)
            except OSError:
                self.lines.append("No se pudieron guardar las preferencias.")
        except (argparse.ArgumentTypeError, ValueError, OSError, RuntimeError) as exc:
            self.notice = str(exc)

    def update_job(self):
        changed = False
        for _ in range(100):
            try:
                line = self.job.events.get_nowait()
            except queue.Empty:
                break
            if line:
                changed = True
                if re.search(r"\d+(?:\.\d+)?%", line) and "error" not in line.lower():
                    self.progress = line
                else:
                    self.lines.append(line)
        code = self.job.poll()
        if code is not None:
            changed = True
            self.status = "CANCELADA" if self.job.cancelled else "COMPLETADA" if code == 0 else "FINALIZADA CON ERRORES"
            self.notice = "Podés volver a descargar: el historial omite los temas completados."
            self.focus = self.FIELDS.index("download")
        return changed

    def activate(self, key):
        if key == "profile":
            self.cycle_profile()
        elif key == "download-tab":
            self.page = "download"
        elif key == "download":
            self.start_download()
        elif key == "cancel":
            self.cancel()
        elif key == "library":
            self.page = "library"
            self.refresh_library()
        elif key == "quit":
            self.quit()
        elif key == "single":
            self.single = not self.single
        elif key.startswith("quality:"):
            self.quality = key.split(":")[1]
            self.focus = self.FIELDS.index("quality")
        elif key in self.FIELDS:
            self.focus = self.FIELDS.index(key)

    def cycle_profile(self):
        if self.job.active:
            self.notice = "Podés cambiar la velocidad al terminar o cancelar la descarga."
            return
        profiles = tuple(PROFILES)
        self.profile = profiles[(profiles.index(self.profile) + 1) % len(profiles)]
        self.notice = "Velocidad: " + PROFILES[self.profile][0] + ". F7 cambia el perfil."
        try:
            save_settings(Path(self.values["folder"]).expanduser(), self.quality, profile=self.profile)
        except OSError:
            self.notice += " No se pudo guardar la preferencia."

    def cancel(self):
        if self.job.active:
            self.job.cancel()
            self.status = "CANCELANDO…"
            self.notice = "Deteniendo la descarga y la conversión…"
        else:
            self.notice = "No hay ninguna descarga en curso."

    def quit(self):
        if self.job.active:
            self.confirm_exit = True
            self.notice = "Hay una descarga en curso. ¿Cancelar y salir? S / N"
        else:
            self.running = False

    def edit(self, key, field):
        value, cursor = self.values[field], self.cursors[field]
        if key in (curses.KEY_BACKSPACE, "\x7f", "\b"):
            if cursor:
                value, cursor = value[:cursor - 1] + value[cursor:], cursor - 1
        elif key == curses.KEY_DC:
            value = value[:cursor] + value[cursor + 1:]
        elif key == curses.KEY_LEFT:
            cursor = max(0, cursor - 1)
        elif key == curses.KEY_RIGHT:
            cursor = min(len(value), cursor + 1)
        elif key in (curses.KEY_HOME, "\x01"):
            cursor = 0
        elif key in (curses.KEY_END, "\x05"):
            cursor = len(value)
        elif key == "\x15":
            value, cursor = "", 0
        elif isinstance(key, str) and key.isprintable():
            value, cursor = value[:cursor] + key + value[cursor:], cursor + len(key)
        self.values[field], self.cursors[field] = value, cursor

    def handle_key(self, key):
        if self.confirm_exit:
            if key in ("s", "S"):
                self.running = False
            elif key in ("n", "N", "\x1b"):
                self.confirm_exit = False
                self.notice = "La descarga continúa."
            return
        if key in (curses.KEY_F10, "\x11", "\x03"):
            self.quit()
            return
        if key == curses.KEY_RESIZE:
            return
        if key == curses.KEY_F7:
            self.cycle_profile()
            return
        if key == curses.KEY_MOUSE:
            try:
                _, x, y, _, state = curses.getmouse()
                if state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED):
                    for row, left, right, name in self.hits:
                        if row == y and left <= x < right:
                            self.activate(name)
                            break
            except curses.error:
                pass
            return
        if self.page == "library":
            if key in ("\x1b", curses.KEY_F6):
                self.page = "download"
                self.notice = "Tab cambia de campo. F5 inicia la descarga."
            elif key in (curses.KEY_UP, curses.KEY_DOWN):
                delta = -1 if key == curses.KEY_UP else 1
                self.library_index = max(0, min(len(self.files) - 1, self.library_index + delta))
            elif key in ("r", "R"):
                self.refresh_library()
            return
        if key == curses.KEY_F5:
            self.start_download()
        elif key == curses.KEY_F6:
            self.activate("library")
        elif key == "\x1b":
            self.cancel()
        elif key in ("\t", curses.KEY_DOWN):
            self.focus = (self.focus + 1) % len(self.FIELDS)
        elif key in (curses.KEY_BTAB, curses.KEY_UP):
            self.focus = (self.focus - 1) % len(self.FIELDS)
        elif key in ("\n", "\r", curses.KEY_ENTER):
            field = self.FIELDS[self.focus]
            if field in self.values:
                self.focus += 1
            elif field == "quality":
                self.focus += 1
            else:
                self.activate(field)
        elif self.FIELDS[self.focus] in self.values:
            self.edit(key, self.FIELDS[self.focus])
        elif self.FIELDS[self.focus] == "quality" and key in (curses.KEY_LEFT, curses.KEY_RIGHT, " "):
            delta = -1 if key == curses.KEY_LEFT else 1
            self.quality = QUALITIES[(QUALITIES.index(self.quality) + delta) % len(QUALITIES)]
        elif self.FIELDS[self.focus] == "single" and key == " ":
            self.single = not self.single

    def run(self):
        # Permite recibir Ctrl+Q y Ctrl+C sin que el terminal los intercepte.
        curses.raw()
        curses.set_escdelay(25)
        self.screen.keypad(True)
        self.screen.timeout(100)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for pair, (foreground, background) in PALETTE.items():
                if curses.COLORS < 256:
                    foreground = curses.COLOR_BLACK if pair == 9 else curses.COLOR_CYAN if pair in (1, 10, 14, 16) else curses.COLOR_WHITE
                    background = curses.COLOR_CYAN if pair == 9 else curses.COLOR_BLACK
                curses.init_pair(pair, foreground, background)
            self.screen.bkgd(" ", curses.color_pair(6))
        try:
            curses.mousemask(curses.ALL_MOUSE_EVENTS)
        except curses.error:
            pass
        try:
            dirty = True
            last_draw = 0
            while self.running:
                dirty = self.update_job() or dirty
                now = time.monotonic()
                if dirty or (self.job.active and now - last_draw >= 0.25):
                    self.draw()
                    dirty = False
                    last_draw = now
                try:
                    self.handle_key(self.screen.get_wch())
                    dirty = True
                except curses.error:
                    continue
                except KeyboardInterrupt:
                    self.quit()
                    dirty = True
        finally:
            self.job.close()


def main():
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("La interfaz necesita una terminal interactiva. Para usar comandos: ./mp3 --help", file=sys.stderr)
        return 1
    try:
        curses.wrapper(lambda screen: TerminalApp(screen).run())
    except (curses.error, KeyboardInterrupt) as exc:
        print(f"No se pudo mantener la interfaz: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
