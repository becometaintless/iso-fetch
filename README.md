# iso-fetch

Descarga la imagen de instalación **amd64 / x86_64** vigente de cada distro. No trae versiones ni rutas absolutas escritas en el script: lee el índice del proyecto en el momento y guarda los archivos en `isos/` (o en el directorio de `ISO_OUT` / `--out`), relativo al directorio de trabajo.

Si el índice oficial falla, prueba los GitHub Releases del upstream y se queda solo con un asset o un enlace cuyo nombre es una imagen amd64. Debian, Ubuntu, Kubuntu y Kali no publican la ISO en GitHub; en ese caso el fallback lo dice y no baja un archivo de un tercero.

| id | qué elige |
| --- | --- |
| `omarchy` | ISO estable x86_64 de `latest.json` |
| `debian-13` | netinst amd64 de Debian 13 |
| `arch` | ISO fechada `x86_64` |
| `ubuntu-24.04` | desktop amd64 más nueva de la serie 24.04 |
| `ubuntu-lts` | desktop amd64 de la LTS que Ubuntu marca como soportada |
| `kali` | `installer-amd64.iso` |
| `kubuntu` | desktop amd64 de la misma serie LTS |
| `bazzite` | `bazzite-stable-amd64.iso` |
| `steamos` | ISO si aparece; si no, la imagen `steamdeck-repair` o `steamdeck-oobe-repair` más nueva (Valve no publica ISO) |
| `nobara` | ediciones Official y GNOME (las dos imágenes x86_64 del sitio) |

```bash
python3 iso_fetch.py list
python3 iso_fetch.py resolve
python3 iso_fetch.py download arch kali
python3 iso_fetch.py download -v
```

Una distro que falle no corta el resto. `--strict` borra la imagen si el sha256 no coincide; sin esa opción la guarda y avisa. La descarga se puede reanudar con el `.part` que queda en el directorio de salida.
