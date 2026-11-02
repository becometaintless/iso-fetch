#!/usr/bin/env python3
"""Resolve and download current amd64 Linux install images.

Paths stay relative to the working directory. Versions come from each
project's index (or GitHub Releases when that index fails), so a point
release does not require editing this file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

UA = "iso-fetch/1.0 (+https://github.com/iso-fetch)"
TIMEOUT = 45
CHUNK = 1024 * 1024

# amd64 / x86_64 only. "x86" covers Omarchy's x86 ISO name, not i386/i686.
ARCH_OK = re.compile(r"(amd64|x86_64|x64|(?<![0-9])x86(?!_64))", re.I)
ARCH_REJECT = re.compile(
    r"(aarch64|arm64|armhf|armel|i386|i686|ppc64|riscv|s390x|source)",
    re.I,
)
IMAGE_EXT = re.compile(r"\.(?:iso|img(?:\.bz2|\.zip)?)$", re.I)
SUM_LINE = re.compile(
    r"^([0-9a-fA-F]{64})\s+\*?(\S+)\s*$",
    re.M,
)
HREF = re.compile(r"""href=["']([^"'#?]+)["']""", re.I)
URL_IN_TEXT = re.compile(r"https?://[^\s)\]<>\"']+")


@dataclass(frozen=True)
class Image:
    distro: str
    url: str
    filename: str
    sha256: str | None
    via: str

    def describe(self) -> str:
        digest = self.sha256 or "checksum unavailable"
        return f"{self.distro}\t{self.via}\t{digest}\t{self.url}"


class ResolveError(Exception):
    pass


def _request(url: str, method: str = "GET", headers: dict | None = None, data: bytes | None = None):
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    return urllib.request.urlopen(req, timeout=TIMEOUT)


def fetch_bytes(url: str, limit: int | None = 2_000_000) -> tuple[str, bytes]:
    last: Exception | None = None
    for attempt in range(3):
        try:
            with _request(url) as resp:
                final = resp.geturl()
                if limit is None:
                    return final, resp.read()
                return final, resp.read(limit)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
            time.sleep(0.4 * (attempt + 1))
    raise ResolveError(f"{url}: {last}")


def fetch_text(url: str, limit: int | None = 2_000_000) -> tuple[str, str]:
    final, raw = fetch_bytes(url, limit)
    return final, raw.decode("utf-8", "replace")


def is_amd64_name(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    if ARCH_REJECT.search(base):
        return False
    if ARCH_OK.search(base):
        return True
    # Some projects publish a single x86_64 image and omit the arch token.
    return False


def filename_of(url: str) -> str:
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    if not name or name in {".", "/"}:
        raise ResolveError(f"no filename in {url}")
    return name


def _version_key(name: str) -> tuple:
    nums = tuple(int(n) for n in re.findall(r"\d+", name))
    return nums


def parse_sums(text: str) -> list[tuple[str, str]]:
    return [(digest.lower(), name) for digest, name in SUM_LINE.findall(text)]


def pick_named(entries: list[tuple[str | None, str]], predicate) -> tuple[str | None, str]:
    matches = [(digest, name) for digest, name in entries if predicate(name)]
    if not matches:
        raise ResolveError("no amd64 image matched")
    matches.sort(key=lambda item: _version_key(item[1]))
    return matches[-1]


def github_images(repo: str) -> list[tuple[str, str | None]]:
    """ISO/image URLs from release assets and from links in the release body."""
    api = f"https://api.github.com/repos/{repo}/releases?per_page=15"
    try:
        _, raw = fetch_bytes(api, limit=4_000_000)
        releases = json.loads(raw.decode("utf-8", "replace"))
    except (ResolveError, json.JSONDecodeError, UnicodeError) as exc:
        raise ResolveError(f"github {repo}: {exc}") from exc
    if not isinstance(releases, list):
        raise ResolveError(f"github {repo}: unexpected payload")

    found: list[tuple[str, str | None, bool]] = []
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        prerelease = bool(rel.get("prerelease"))
        for asset in rel.get("assets") or []:
            if not isinstance(asset, dict):
                continue
            name = str(asset.get("name") or "")
            url = str(asset.get("browser_download_url") or "")
            if url and IMAGE_EXT.search(name) and is_amd64_name(name):
                found.append((url, None, prerelease))
        body = str(rel.get("body") or "")
        for url in URL_IN_TEXT.findall(body):
            url = url.rstrip(".,)")
            name = filename_of(url) if "/" in url else url
            if IMAGE_EXT.search(name) and is_amd64_name(name) and ARCH_REJECT.search(name) is None:
                found.append((url, None, prerelease))
            elif IMAGE_EXT.search(name) and "aarch64" not in name.lower() and "arm64" not in name.lower():
                # Omarchy stable ISOs are named omarchy-<version>.iso (x86_64 channel).
                if name.lower().startswith("omarchy-") and "aarch64" not in url.lower():
                    found.append((url, None, prerelease))
    stable = [item for item in found if not item[2]]
    chosen = stable or found
    # de-dupe, newest version last so callers can take [-1]
    uniq: dict[str, tuple[str, str | None]] = {}
    for url, digest, _pre in chosen:
        uniq[url] = (url, digest)
    ordered = list(uniq.values())
    ordered.sort(key=lambda item: _version_key(filename_of(item[0])))
    if not ordered:
        raise ResolveError(f"github {repo}: no amd64 image in recent releases")
    return ordered


def _from_sums(distro: str, sums_url: str, predicate, via: str) -> Image:
    final, text = fetch_text(sums_url)
    digest, name = pick_named(parse_sums(text), predicate)
    base = final.rsplit("/", 1)[0] + "/"
    url = urllib.parse.urljoin(base, urllib.parse.quote(name))
    return Image(distro, url, name, digest, via)


def resolve_arch() -> Image:
    index = "https://geo.mirror.pkgbuild.com/iso/latest/"
    final, text = fetch_text(index)
    names = [urllib.parse.unquote(href) for href in HREF.findall(text)]
    iso_names = [
        name
        for name in names
        if name.endswith(".iso")
        and "x86_64" in name
        and "bootstrap" not in name
        and re.search(r"\d{4}\.\d{2}\.\d{2}", name)
    ]
    if not iso_names:
        raise ResolveError("arch index had no dated x86_64 iso")
    name = sorted(iso_names, key=_version_key)[-1]
    base = final if final.endswith("/") else final + "/"
    digest = None
    for sums_name in ("sha256sums.txt", "sha256sums"):
        try:
            _, sums = fetch_text(urllib.parse.urljoin(base, sums_name))
        except ResolveError:
            continue
        for candidate, summed_name in parse_sums(sums):
            if summed_name == name:
                digest = candidate
                break
        if digest:
            break
    return Image("arch", urllib.parse.urljoin(base, name), name, digest, "mirror-index")


def resolve_debian13() -> Image:
    predicate = lambda name: bool(re.fullmatch(r"debian-13\.[0-9.]+-amd64-netinst\.iso", name))
    current = "https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/SHA256SUMS"
    try:
        return _from_sums("debian-13", current, predicate, "cdimage-current")
    except ResolveError:
        pass
    _, listing = fetch_text("https://cdimage.debian.org/debian-cd/")
    versions = sorted(set(re.findall(r"href=\"(13\.[0-9.]+)/\"", listing)), key=_version_key)
    if not versions:
        raise ResolveError("no debian 13 tree on cdimage")
    url = f"https://cdimage.debian.org/debian-cd/{versions[-1]}/amd64/iso-cd/SHA256SUMS"
    return _from_sums("debian-13", url, predicate, "cdimage-13")


def _ubuntu_lts_series() -> str:
    _, text = fetch_text("https://changelogs.ubuntu.com/meta-release-lts")
    series = None
    major = None
    for block in text.split("\n\n"):
        fields = {}
        for line in block.splitlines():
            if ":" in line:
                key, val = line.split(":", 1)
                fields[key.strip()] = val.strip()
        if fields.get("Supported") == "1" and fields.get("Version") and fields.get("Dist"):
            series = fields["Dist"]
            major = fields["Version"].split()[0]
    if not series or not major:
        raise ResolveError("could not read the supported Ubuntu LTS from meta-release-lts")
    # Directory names on releases.ubuntu.com use YY.MM, not the point release.
    return ".".join(major.split(".")[:2])


def _ubuntu_desktop(distro: str, series: str) -> Image:
    return _from_sums(
        distro,
        f"https://releases.ubuntu.com/{series}/SHA256SUMS",
        lambda name: bool(re.fullmatch(rf"ubuntu-{re.escape(series)}\.[0-9]+-desktop-amd64\.iso", name)),
        "releases.ubuntu.com",
    )


def resolve_ubuntu_2404() -> Image:
    return _ubuntu_desktop("ubuntu-24.04", "24.04")


def resolve_ubuntu_lts() -> Image:
    return _ubuntu_desktop("ubuntu-lts", _ubuntu_lts_series())


def resolve_kubuntu() -> Image:
    series = _ubuntu_lts_series()
    return _from_sums(
        "kubuntu",
        f"https://cdimage.ubuntu.com/kubuntu/releases/{series}/release/SHA256SUMS",
        lambda name: bool(re.fullmatch(rf"kubuntu-{re.escape(series)}\.[0-9]+-desktop-amd64\.iso", name)),
        "cdimage.ubuntu.com",
    )


def resolve_kali() -> Image:
    return _from_sums(
        "kali",
        "https://cdimage.kali.org/current/SHA256SUMS",
        lambda name: bool(re.fullmatch(r"kali-linux-[0-9.]+-installer-amd64\.iso", name)),
        "cdimage.kali.org",
    )


def resolve_omarchy() -> Image:
    _, raw = fetch_bytes("https://nightly.omarchy.org/stable/latest.json", limit=200_000)
    meta = json.loads(raw.decode("utf-8", "replace"))
    url = str(meta["url"])
    name = filename_of(url)
    if ARCH_REJECT.search(name) or "aarch64" in url.lower():
        raise ResolveError(f"stable omarchy url is not x86_64: {url}")
    digest = str(meta.get("sha256") or "") or None
    return Image("omarchy", url, name, digest, "stable-latest.json")


def resolve_bazzite() -> Image:
    url = "https://download.bazzite.gg/bazzite-stable-amd64.iso"
    # Confirm the rolling name still exists. Some mirrors reject HEAD; a 405
    # still means the path is present, so only 404 fails this check.
    try:
        with _request(url, method="HEAD") as resp:
            if resp.status == 404:
                raise ResolveError("bazzite stable amd64 iso is missing")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ResolveError("bazzite stable amd64 iso is missing") from exc
        if exc.code not in {405, 403}:
            raise ResolveError(f"bazzite: {exc}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ResolveError(f"bazzite: {exc}") from exc
    return Image("bazzite", url, filename_of(url), None, "download.bazzite.gg")


def resolve_steamos() -> Image:
    index = "https://steamdeck-images.steamos.cloud/recovery/"
    final, text = fetch_text(index)
    names = [urllib.parse.unquote(Path(href).name) for href in HREF.findall(text)]
    isos = [name for name in names if name.endswith(".iso") and "steamframe" not in name.lower()]
    if isos:
        name = sorted(isos, key=_version_key)[-1]
        via = "recovery-index-iso"
    else:
        repairs = [
            name
            for name in names
            if re.fullmatch(r"steamdeck-(?:oobe-)?repair-.+\.img\.bz2", name)
        ]
        if not repairs:
            raise ResolveError("steamos recovery index has no steamdeck repair image")
        name = sorted(repairs, key=_version_key)[-1]
        via = "recovery-index"
    base = final if final.endswith("/") else final + "/"
    return Image("steamos", urllib.parse.urljoin(base, urllib.parse.quote(name)), name, None, via)


def resolve_nobara() -> list[Image]:
    """Official (KDE) and GNOME. Nobara publishes one x86_64 ISO per edition."""
    page = "https://www.nobaraproject.org/download.html"
    final, text = fetch_text(page, limit=1_000_000)
    wanted = ("official", "gnome")
    found: dict[str, Image] = {}
    for href in HREF.findall(text):
        if ".iso" not in href.lower():
            continue
        absolute = urllib.parse.urljoin(final, href)
        lower = absolute.lower()
        if "aarch64" in lower or "arm64" in lower:
            continue
        try:
            name = filename_of(absolute)
        except ResolveError:
            continue
        edition = None
        for token in wanted:
            if f"-{token}-" in name.lower() or f"-{token}." in name.lower():
                edition = token
        if edition is None:
            continue
        if name.endswith(".iso.sha256sum"):
            iso_name = name[: -len(".sha256sum")]
            iso_url = absolute[: -len(".sha256sum")]
            digest = None
            try:
                _, body = fetch_text(absolute, limit=200_000)
                digest = parse_sums(body)[0][0] if parse_sums(body) else body.split()[0].lower()
                if not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
                    digest = None
            except (ResolveError, IndexError):
                digest = None
            found[edition] = Image(f"nobara-{edition}", iso_url, iso_name, digest, "nobaraproject.org")
        elif name.endswith(".iso") and edition not in found:
            found[edition] = Image(f"nobara-{edition}", absolute, name, None, "nobaraproject.org")
    images = [found[token] for token in wanted if token in found]
    if len(images) != 2:
        raise ResolveError("nobara page did not list both Official and GNOME isos")
    return images


PRIMARY = {
    "omarchy": resolve_omarchy,
    "debian-13": resolve_debian13,
    "arch": resolve_arch,
    "ubuntu-24.04": resolve_ubuntu_2404,
    "ubuntu-lts": resolve_ubuntu_lts,
    "kali": resolve_kali,
    "kubuntu": resolve_kubuntu,
    "bazzite": resolve_bazzite,
    "steamos": resolve_steamos,
    "nobara": resolve_nobara,
}

# Official upstream repos only. Debian, Ubuntu, Kubuntu, and Kali publish
# install ISOs on their own mirrors, not as GitHub Release assets, so those
# ids have an empty list and the fallback reports that instead of taking a
# third-party file.
GITHUB_REPOS = {
    "omarchy": ["omacom/omarchy", "omacom/omarchy-iso"],
    "debian-13": [],
    "arch": ["archlinux/archiso"],
    "ubuntu-24.04": [],
    "ubuntu-lts": [],
    "kali": [],
    "kubuntu": [],
    "bazzite": ["ublue-os/bazzite"],
    "steamos": ["ValveSoftware/SteamOS"],
    "nobara": ["Nobara-Project/nobara-images"],
}


def _github_for(distro: str, predicate) -> Image:
    repos = list(GITHUB_REPOS.get(distro, []))
    if not repos:
        raise ResolveError(f"{distro}: upstream does not publish install images on GitHub Releases")
    errors = []
    candidates: list[tuple[str, str | None]] = []
    for repo in dict.fromkeys(repos):
        try:
            candidates.extend(github_images(repo))
        except ResolveError as exc:
            errors.append(str(exc))
    matched = []
    for url, digest in candidates:
        name = filename_of(url)
        try:
            if predicate(name):
                matched.append((digest, name, url))
        except ResolveError:
            continue
    if not matched:
        detail = "; ".join(errors) or "no matching asset"
        raise ResolveError(detail)
    matched.sort(key=lambda item: _version_key(item[1]))
    digest, name, url = matched[-1]
    return Image(distro if distro != "nobara" else name, url, name, digest, "github-releases")


def _predicate(distro: str):
    if distro == "debian-13":
        return lambda name: bool(re.search(r"debian-.*amd64.*\.iso$", name, re.I)) and "edu" not in name and "mac" not in name
    if distro == "arch":
        return lambda name: name.endswith(".iso") and "x86_64" in name and "bootstrap" not in name
    if distro == "ubuntu-24.04":
        return lambda name: bool(re.search(r"ubuntu-24\.04(\.[0-9]+)?-desktop-amd64\.iso$", name))
    if distro == "ubuntu-lts":
        return lambda name: bool(re.search(r"ubuntu-[0-9]+\.[0-9]+(\.[0-9]+)?-desktop-amd64\.iso$", name))
    if distro == "kubuntu":
        return lambda name: bool(re.search(r"kubuntu-.*desktop-amd64\.iso$", name))
    if distro == "kali":
        return lambda name: bool(re.search(r"kali-linux-.*installer-amd64\.iso$", name))
    if distro == "omarchy":
        return lambda name: name.endswith(".iso") and "aarch64" not in name.lower()
    if distro == "bazzite":
        return lambda name: name.endswith(".iso") and "amd64" in name.lower() and "arm" not in name.lower()
    if distro == "steamos":
        return lambda name: name.endswith(".iso") or bool(
            re.search(r"steamdeck-(?:oobe-)?repair-.+\.img\.bz2$", name)
        )
    if distro == "nobara":
        return lambda name: bool(re.search(r"Nobara-.*-(Official|GNOME)-.*\.iso$", name))
    raise ResolveError(distro)


def resolve_one(distro: str) -> list[Image]:
    if distro not in PRIMARY:
        raise ResolveError(f"unknown distro {distro}. known: {', '.join(PRIMARY)}")
    try:
        primary = PRIMARY[distro]()
        images = primary if isinstance(primary, list) else [primary]
        if not images:
            raise ResolveError("empty result")
        return images
    except (ResolveError, KeyError, json.JSONDecodeError, urllib.error.URLError, OSError, ValueError) as primary_exc:
        try:
            if distro == "nobara":
                images = []
                errors = []
                for edition, pred in (
                    ("official", lambda name: bool(re.search(r"Nobara-.*-Official-.*\.iso$", name))),
                    ("gnome", lambda name: bool(re.search(r"Nobara-.*-GNOME-.*\.iso$", name, re.I))),
                ):
                    try:
                        image = _github_for("nobara", pred)
                        images.append(
                            Image(f"nobara-{edition}", image.url, image.filename, image.sha256, image.via)
                        )
                    except ResolveError as exc:
                        errors.append(str(exc))
                if len(images) == 2:
                    return images
                raise ResolveError("; ".join(errors))
            return [_github_for(distro, _predicate(distro))]
        except ResolveError as fallback_exc:
            raise ResolveError(f"{distro}: primary failed ({primary_exc}); github fallback failed ({fallback_exc})") from fallback_exc


def resolve_all(names: list[str]) -> tuple[list[Image], list[str]]:
    images: list[Image] = []
    errors: list[str] = []
    for name in names:
        try:
            images.extend(resolve_one(name))
        except ResolveError as exc:
            errors.append(str(exc))
    return images, errors


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def _fmt_bytes(n: float) -> str:
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _fmt_eta(seconds: float) -> str:
    if seconds < 0 or seconds == float("inf"):
        return "?"
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _draw_progress(label: str, written: int, total: int | None, speed: float) -> None:
    width = 28
    speed_txt = f"{_fmt_bytes(speed)}/s" if speed > 0 else "0 B/s"
    if total and total > 0:
        frac = min(written / total, 1.0)
        filled = int(width * frac)
        bar = "#" * filled + "-" * (width - filled)
        left = (total - written) / speed if speed > 0 else float("inf")
        line = (
            f"{label:<16} [{bar}] {frac * 100:5.1f}%  "
            f"{_fmt_bytes(written)}/{_fmt_bytes(total)}  {speed_txt}  eta {_fmt_eta(left)}"
        )
    else:
        line = f"{label:<16} [{'?' * width}]   {_fmt_bytes(written)}  {speed_txt}"
    print("\r" + line, end="", flush=True)


def download(image: Image, out_dir: Path, strict: bool, verbose: bool = False) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / image.filename
    partial = out_dir / (image.filename + ".part")
    if dest.exists() and image.sha256:
        if _sha256_file(dest) == image.sha256:
            print(f"ok  {image.distro}  already complete  {dest}", flush=True)
            return dest
        dest.unlink()
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": UA}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    req = urllib.request.Request(image.url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and partial.exists():
            partial.replace(dest)
            resp = None
        else:
            raise ResolveError(f"{image.distro}: download {exc}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ResolveError(f"{image.distro}: download {exc}") from exc

    if resp is not None:
        status = getattr(resp, "status", 200)
        mode = "ab" if status == 206 and offset else "wb"
        if mode == "wb":
            offset = 0
        total = resp.headers.get("Content-Length")
        try:
            total_n = int(total) + (offset if status == 206 else 0) if total else None
        except ValueError:
            total_n = None
        written = offset
        started = time.monotonic()
        sample_at = started
        sample_bytes = offset
        speed = 0.0
        with partial.open(mode) as handle:
            while True:
                block = resp.read(CHUNK)
                if not block:
                    break
                handle.write(block)
                written += len(block)
                now = time.monotonic()
                delta = now - sample_at
                if delta >= 0.25:
                    speed = (written - sample_bytes) / delta
                    sample_at = now
                    sample_bytes = written
                _draw_progress(image.distro, written, total_n, speed)
        elapsed = max(time.monotonic() - started, 1e-6)
        _draw_progress(image.distro, written, total_n, (written - offset) / elapsed)
        print(flush=True)
        if verbose:
            print(
                f"  { _fmt_bytes(written) } in {elapsed:.1f}s"
                f"  avg {_fmt_bytes((written - offset) / elapsed)}/s",
                flush=True,
            )
        resp.close()
        partial.replace(dest)

    if image.sha256:
        got = _sha256_file(dest)
        if got != image.sha256:
            message = f"{image.distro}: sha256 mismatch got {got} expected {image.sha256}"
            if strict:
                dest.rename(out_dir / (image.filename + ".bad"))
                raise ResolveError(message)
            print(f"warn  {message}", file=sys.stderr, flush=True)
    print(f"saved  {image.distro}  {dest}", flush=True)
    return dest


def default_names() -> list[str]:
    return list(PRIMARY)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download current amd64 Linux install images.")
    parser.add_argument("command", choices=("list", "resolve", "download"))
    parser.add_argument("distros", nargs="*", help="subset of distro ids; default is all")
    parser.add_argument(
        "--out",
        default=os.environ.get("ISO_OUT", "isos"),
        help="download directory, relative to the working directory (default: isos, or ISO_OUT)",
    )
    parser.add_argument("--strict", action="store_true", help="fail the download when a checksum does not match")
    parser.add_argument("-v", "--verbose", action="store_true", help="print source, url, checksum, and average speed")
    args = parser.parse_args(argv)
    names = args.distros or default_names()
    unknown = [name for name in names if name not in PRIMARY]
    if unknown:
        print(f"unknown: {', '.join(unknown)}", file=sys.stderr)
        print("known: " + ", ".join(PRIMARY), file=sys.stderr)
        return 2
    if args.command == "list":
        for name in names:
            print(name)
        return 0
    images, errors = resolve_all(names)
    if args.command == "resolve" or args.verbose:
        for image in images:
            print(image.describe())
    if errors:
        print("\n".join(errors), file=sys.stderr)
    if args.command == "resolve":
        return 1 if errors else 0
    out_dir = Path(args.out)
    failed = False
    for image in images:
        try:
            download(image, out_dir, args.strict, args.verbose)
        except ResolveError as exc:
            failed = True
            print(exc, file=sys.stderr)
    return 1 if failed or errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
