# YouTube / Spotify → MP3

App de terminal en español para descargar canciones y playlists completas de YouTube o Spotify (vivecodeada a dos manos).

## Iniciar

```bash
cd /home/li/Downloads/youtube-mp3
./instalar.sh
./mp3
```

`./mp3` abre una interfaz visual dentro de la terminal. Pegá el enlace, elegí la carpeta y la calidad, y seleccioná **Descargar**.

- `Tab` / `Shift+Tab` o flechas arriba/abajo: cambiar de campo.
- Flechas izquierda/derecha: mover el cursor o elegir calidad.
- `Enter`: avanzar de campo o activar un botón. Espacio marca/desmarca «Solo el video».
- `F5`: comenzar la descarga.
- `Esc`: cancelar la descarga o volver desde la biblioteca.
- `F6`: consultar los MP3 de la carpeta elegida; `R` actualiza la lista.
- `F7`: cambiar el perfil de velocidad (Tranquilo, Equilibrado o Rápido).
- `F10` o `Ctrl+Q`: salir. Si hay una descarga activa, pide confirmación.
- `Ctrl+U`: vaciar el campo de texto seleccionado.

También se pueden pulsar los controles con el mouse si tu terminal lo admite. La interfaz necesita al menos 64 columnas y 24 filas. Usa `curses`, incluido con Python en Linux, sin instalar otra biblioteca visual.

La carpeta y la calidad se recuerdan después de iniciar una descarga. La salida del proceso aparece en vivo y se guarda completa en `.logs/`. Al cancelar, los MP3 completados se conservan; volver a iniciar el mismo enlace permite reintentar las pendientes. El progreso corresponde al archivo actual cuando el descargador informa un porcentaje; la animación indica actividad y no un porcentaje total de la playlist.

La biblioteca permite consultar archivos y sus rutas; no incluye reproducción ni borrado. La autenticación de YouTube sigue pendiente. Se conservan las portadas que agrega Spotify; esta actualización de interfaz no cambia el tratamiento de las miniaturas de YouTube.

La interfaz usa paneles con fondo oscuro y colores propios en terminales de 256 colores. En ventanas amplias aparece un resumen lateral; en ventanas pequeñas se adapta a una sola columna. El borde violeta indica el campo activo y la calidad elegida se destaca en verde. Durante la descarga se muestra una barra cuando hay un porcentaje disponible.

## Velocidad

`F7` cambia el perfil y guarda tu elección. El perfil se aplica a la próxima descarga; no se modifica mientras hay una en curso.

| Perfil | Canciones simultáneas en Spotify | Fragmentos por audio compatible |
| --- | --- | --- |
| Tranquilo | 2 | 1 |
| Equilibrado (inicial) | 4 | 2 |
| Rápido | 6 | 2 |

Spotify procesa varias canciones con el paralelismo de spotDL. En YouTube se mantiene el procesamiento de canciones de la playlist en secuencia, con descarga paralela de fragmentos cuando el formato lo admite. También se empieza a procesar la playlist sin esperar a obtener toda la lista. Más conexiones no garantizan mayor velocidad: usá Tranquilo si tu conexión o equipo se saturan.

Se conservan el MP3, las portadas que ya aporta Spotify y los metadatos musicales. Por defecto se omite la búsqueda adicional de letras en Spotify. Para incluirlas, usá el modo de comandos con `--con-letras`.

```bash
./mp3 'https://open.spotify.com/playlist/PLAYLIST_ID' --paralelas 6 --fragmentos 2
./mp3 'https://open.spotify.com/track/TRACK_ID' --con-letras
./mp3 'https://www.youtube.com/playlist?list=PLAYLIST_ID' --fragmentos 2
```

La interfaz dibuja solo cuando hay cambios, con actualización de la animación durante las descargas. Los registros completos se escriben con un búfer, con vaciado periódico al recibir nuevas líneas y al terminar, para reducir las escrituras pequeñas a disco. Los errores se guardan inmediatamente.

En una medición local de renderizado con una pantalla simulada de 132 × 38, el tiempo de 100 dibujos bajó de 6,83 s a 0,63 s; 1000 recortes de texto bajaron de 2,01 s a 0,061 s. Son mediciones de la interfaz, no de velocidad de descarga. La mejora al descargar depende de la conexión, el formato y los tiempos de respuesta de los servicios.

También podés pasar un enlace directamente para usar el modo de comandos anterior:

```bash
./mp3 'https://www.youtube.com/watch?v=VIDEO_ID'
./mp3 'https://www.youtube.com/playlist?list=PLAYLIST_ID'
./mp3 'https://www.youtube.com/playlist?list=PLAYLIST_ID' --calidad 320 -o ~/Music
./mp3 'https://open.spotify.com/playlist/PLAYLIST_ID'
./mp3 'https://open.spotify.com/track/TRACK_ID' --calidad 192 -o ~/Music
```

Poné los enlaces entre comillas al pasarlos como argumento para conservar caracteres como `&`.

Los MP3 se guardan en `musica`, dentro de una subcarpeta con el nombre de la playlist o `Canciones sueltas`. La calidad predeterminada es 192 kbps; elegir un valor más alto no mejora la calidad del audio original.

Si un enlace de video contiene `list=...`, se descarga la playlist completa. Agregá `--solo-cancion` para descargar únicamente ese video.

Si una canción falla, continúa con las demás y termina indicando el error. Podés ejecutar nuevamente el mismo enlace: el historial `musica/.descargadas.txt` permite omitir las canciones completadas de YouTube. Spotify utiliza un historial independiente en `musica/.descargadas-spotify.txt`. Cada historial es compartido entre playlists del mismo servicio y carpeta de destino: una canción ya descargada no se duplica en otra playlist. Si eliminás un MP3 y querés descargarlo otra vez, quitá su entrada del historial o usá otra carpeta de destino.

## Spotify

Pegá un enlace completo de `open.spotify.com/playlist/...` o `open.spotify.com/track/...`. También se aceptan enlaces con prefijos regionales como `/intl-es/`. Para enlaces acortados `spotify.link`, abrí primero el enlace en tu navegador y copiá la dirección completa.

La app usa [spotDL](https://github.com/spotDL/spotify-downloader) para leer la lista de canciones de Spotify y buscar su audio en YouTube. Los MP3 se guardan en `musica/Spotify`, organizados por playlist, con artista, título, ID y metadatos. Algunas canciones pueden no encontrarse o coincidir con una versión diferente. Se admiten playlists accesibles sin iniciar sesión; no se incluye acceso a playlists privadas.

Si ya tenías la app, ejecutá nuevamente `./instalar.sh` para instalar spotDL. Las opciones `--calidad` y `--carpeta` funcionan con ambos servicios. `--solo-cancion` permite elegir el video de un enlace de YouTube; para Spotify, pasá directamente el enlace de la canción.

Los videos privados, eliminados o restringidos pueden no estar disponibles. Las playlists deben ser accesibles sin iniciar sesión. La app no accede a las cookies del navegador.

## Dependencias y actualizaciones

Requiere Python 3.11 a 3.15, FFmpeg (con ffprobe), y Node.js 22 o superior o Deno. `instalar.sh` instala o actualiza yt-dlp, spotDL y sus componentes en un entorno virtual local. Repetí `./instalar.sh` si los servicios cambian y las descargas dejan de funcionar. Elegir un bitrate alto no mejora la calidad del audio disponible en YouTube.

Implementado con [yt-dlp](https://github.com/yt-dlp/yt-dlp), FFmpeg y la [configuración oficial de JavaScript para YouTube](https://github.com/yt-dlp/yt-dlp/wiki/EJS).
