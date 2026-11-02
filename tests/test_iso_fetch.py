import hashlib
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import iso_fetch


SUMS = """
aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa *ubuntu-24.04.3-desktop-amd64.iso
bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb *ubuntu-24.04.4-desktop-amd64.iso
cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc *ubuntu-24.04.4-live-server-amd64.iso
dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd *ubuntu-24.04.4-desktop-arm64.iso
"""


class ParseTests(unittest.TestCase):
    def test_pick_newest_desktop_amd64(self):
        digest, name = iso_fetch.pick_named(
            iso_fetch.parse_sums(SUMS),
            lambda item: item.endswith("desktop-amd64.iso"),
        )
        self.assertEqual(name, "ubuntu-24.04.4-desktop-amd64.iso")
        self.assertEqual(digest, "b" * 64)

    def test_rejects_other_arch(self):
        self.assertTrue(iso_fetch.is_amd64_name("kali-linux-2026.3-installer-amd64.iso"))
        self.assertTrue(iso_fetch.is_amd64_name("archlinux-2026.11.01-x86_64.iso"))
        self.assertTrue(iso_fetch.is_amd64_name("omarchy-4.5.0-rc2-x86.iso"))
        self.assertFalse(iso_fetch.is_amd64_name("kali-linux-2026.3-installer-arm64.iso"))
        self.assertFalse(iso_fetch.is_amd64_name("debian-13.7.0-i386-netinst.iso"))
        self.assertFalse(iso_fetch.is_amd64_name("omarchy-4.5.0-rc2-aarch64.iso"))

    def test_ubuntu_lts_series_from_meta(self):
        text = "Dist: jammy\nVersion: 22.04.5 LTS\nSupported: 0\n\nDist: noble\nVersion: 24.04.3 LTS\nSupported: 1\n"
        original = iso_fetch.fetch_text
        iso_fetch.fetch_text = lambda *a, **k: ("https://example", text)
        try:
            self.assertEqual(iso_fetch._ubuntu_lts_series(), "24.04")
        finally:
            iso_fetch.fetch_text = original


class DownloadTests(unittest.TestCase):
    def test_download_resumes_and_checks_sum(self):
        payload = b"amd64-image-bytes"
        digest = hashlib.sha256(payload).hexdigest()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                start = 0
                status = 200
                data = payload
                header = self.headers.get("Range")
                if header:
                    start = int(header.split("=")[1].split("-")[0])
                    data = payload[start:]
                    status = 206
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, fmt, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        out = Path("tests") / "_tmp_download"
        out.mkdir(parents=True, exist_ok=True)
        partial = out / "demo.iso.part"
        partial.write_bytes(payload[:4])
        image = iso_fetch.Image(
            "demo",
            f"http://127.0.0.1:{server.server_address[1]}/demo.iso",
            "demo.iso",
            digest,
            "test",
        )
        try:
            saved = iso_fetch.download(image, out, strict=True)
            self.assertEqual(saved.read_bytes(), payload)
            self.assertFalse(partial.exists())
        finally:
            server.shutdown()
            server.server_close()
            for child in out.iterdir():
                child.unlink()
            out.rmdir()


if __name__ == "__main__":
    raise SystemExit(unittest.main())
