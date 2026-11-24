#!/usr/bin/env python3
"""Descarga canciones o playlists de YouTube y Spotify como MP3."""

import argparse
from importlib.util import find_spec
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import parse_qs, urlparse


BASE = Path(__file__).resolve().parent


def music_url(value):
    value = value.strip()
    if not value.startswith(("https://", "http://")):
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.hostname == "open.spotify.com":
        parts = parsed.path.strip("/").split("/")
        # Spotify también comparte enlaces con un prefijo como /intl-es/.
        if parts[0].startswith("intl-"):
            parts = parts[1:]
        if len(parts) == 2 and parts[0] in {"track", "playlist"} and parts[1].isascii() and parts[1].isalnum():
            return f"https://open.spotify.com/{parts[0]}/{parts[1]}"
        raise argparse.ArgumentTypeError("El enlace de Spotify debe apuntar a una canción o playlist.")
    try:
        return youtube_url(value)
    except argparse.ArgumentTypeError as exc:
        raise argparse.ArgumentTypeError("Pegá un enlace de canción o playlist de YouTube o Spotify (open.spotify.com).") from exc


def is_spotify(url):
    return urlparse(url).hostname == "open.spotify.com"


def youtube_url(value):
    value = value.strip()
    if not value.startswith(("https://", "http://")):
        value = "https://" + value
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if host not in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"}:
        raise argparse.ArgumentTypeError("Pegá un enlace válido de YouTube o YouTube Music.")
    query = parse_qs(parsed.query)
    is_video = (parsed.path == "/watch" and query.get("v")) or (
        parsed.path.startswith(("/shorts/", "/live/", "/embed/"))
        and len(parsed.path.strip("/").split("/")) >= 2
    ) or (host.endswith("youtu.be") and parsed.path.strip("/"))
    if not is_video and not (parsed.path == "/playlist" and query.get("list")):
        raise argparse.ArgumentTypeError("El enlace debe apuntar a una canción o una playlist.")
    return value


def build_command(url, output, quality, only_video=False, fragments=2):
    # El ID evita colisiones entre canciones distintas que tengan el mismo título.
    template = "%(playlist_title|Canciones sueltas)s/%(title).150B [%(id)s].%(ext)s"
    command = [
        sys.executable, "-m", "yt_dlp", "--ignore-config",
        "--no-playlist" if only_video else "--yes-playlist",
        "--no-abort-on-error", "--no-overwrites", "--windows-filenames",
        "--format", "bestaudio/best", "--extract-audio", "--audio-format", "mp3",
        "--audio-quality", str(quality), "--embed-metadata", "--newline",
        "--lazy-playlist", "--concurrent-fragments", str(fragments),
        "--progress", "--progress-delta", "0.5",
        "--socket-timeout", "30", "--retries", "3",
        "--download-archive", str(output / ".descargadas.txt"),
        "--paths", str(output), "--output", template,
        "--print", "after_move:MP3 guardado: %(filepath)s",
    ]
    if shutil.which("node"):
        command.extend(["--js-runtimes", "node"])
    command.extend(["--", url])
    return command


def build_spotify_command(url, output, quality, errors, parallel=4, fragments=2, lyrics=False):
    folder = "Spotify/{list-name}" if "/playlist/" in urlparse(url).path else "Spotify/Canciones sueltas"
    command = [
        sys.executable, "-m", "spotdl", "download", url,
        "--format", "mp3", "--bitrate", f"{quality}k",
        "--output", str(output / folder / "{artists} - {title} [{track-id}].{output-ext}"),
        "--overwrite", "skip", "--archive", str(output / ".descargadas-spotify.txt"),
        "--print-errors", "--save-errors", str(errors),
        "--threads", str(parallel), "--simple-tui",
        "--max-filename-length", "180",
    ]
    if not lyrics:
        # Una lista vacía evita búsquedas adicionales de letras; se conservan
        # la portada y los metadatos musicales.
        command.append("--lyrics")
    downloader_args = f"--concurrent-fragments {fragments} --socket-timeout 30"
    if shutil.which("node"):
        downloader_args += " --js-runtimes node"
    command.append("--yt-dlp-args=" + downloader_args)
    return command


def download_spotify(url, output, quality, parallel=4, fragments=2, lyrics=False):
    # spotDL puede terminar con código 0 aunque fallen canciones individuales.
    # Un informe nuevo por ejecución evita confundir errores viejos con actuales.
    with tempfile.TemporaryDirectory(prefix="spotify-", dir=output) as temporary:
        errors = Path(temporary) / "errores.txt"
        result = subprocess.run(build_spotify_command(url, output, quality, errors, parallel, fragments, lyrics), check=False)
        if errors.exists() and errors.read_text(encoding="utf-8").strip():
            print("\nCanciones con errores:\n" + errors.read_text(encoding="utf-8"), file=sys.stderr)
            return 1
        return result.returncode


def main():
    parser = argparse.ArgumentParser(description="Descargá una canción o una playlist completa de YouTube o Spotify en MP3.")
    parser.add_argument("url", nargs="?", type=music_url, help="Enlace de canción o playlist de YouTube o Spotify")
    parser.add_argument("-o", "--carpeta", type=Path, default=BASE / "musica", help="Carpeta de destino (predeterminada: musica)")
    parser.add_argument("--calidad", choices=("128", "192", "256", "320"), default="192", help="Calidad MP3 en kbps (predeterminada: 192)")
    parser.add_argument("--solo-cancion", action="store_true", help="Descargar solo el video si el enlace incluye una playlist")
    parser.add_argument("--paralelas", type=int, choices=range(1, 9), default=4, metavar="1-8", help="Canciones simultáneas en Spotify (predeterminado: 4)")
    parser.add_argument("--fragmentos", type=int, choices=range(1, 5), default=2, metavar="1-4", help="Fragmentos simultáneos por audio compatible (predeterminado: 2)")
    parser.add_argument("--con-letras", action="store_true", help="Buscar también letras en Spotify (puede demorar más)")
    args = parser.parse_args()
    try:
        if args.url is None:
            print("♫ YouTube / Spotify → MP3 | Canciones y playlists\n")
            try:
                args.url = music_url(input("Pegá el enlace de YouTube o Spotify: "))
            except argparse.ArgumentTypeError as exc:
                print(f"Error: {exc}", file=sys.stderr)
                return 2
        if is_spotify(args.url) and args.solo_cancion and "/playlist/" in args.url:
            print("Para descargar una sola canción de Spotify, pegá su enlace de canción en lugar de la playlist.", file=sys.stderr)
            return 2
        dependency = "spotdl" if is_spotify(args.url) else "yt_dlp"
        if find_spec(dependency) is None:
            print(f"Falta instalar {dependency}. Ejecutá ./instalar.sh desde la carpeta de la app.", file=sys.stderr)
            return 1
        missing = [name for name in ("ffmpeg", "ffprobe") if not shutil.which(name)]
        if missing:
            print("Falta FFmpeg. Instalalo con el gestor de paquetes de tu sistema.", file=sys.stderr)
            return 1
        if not (shutil.which("node") or shutil.which("deno")):
            print("Falta Node.js (22 o superior) o Deno para descargar desde YouTube.", file=sys.stderr)
            return 1
        output = args.carpeta.expanduser().resolve()
        output.mkdir(parents=True, exist_ok=True)
        print(f"\nDestino: {output}\nDescargando y convirtiendo a MP3…", flush=True)
        if is_spotify(args.url):
            print("Leyendo Spotify y buscando las canciones en YouTube…", flush=True)
            returncode = download_spotify(args.url, output, args.calidad, args.paralelas, args.fragmentos, args.con_letras)
        else:
            returncode = subprocess.run(build_command(args.url, output, args.calidad, args.solo_cancion, args.fragmentos), check=False).returncode
        if returncode:
            print("\nHubo errores. Las canciones completadas se conservaron. Podés volver a pasar el enlace para reintentar las pendientes.", file=sys.stderr)
            return 1
        print(f"\nListo. Tus MP3 están en: {output}")
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\nDescarga cancelada.")
        return 130
    except OSError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
