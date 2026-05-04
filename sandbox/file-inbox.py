#!/usr/bin/env python3
"""Per-sandbox file inbox.

Tiny stdlib-only HTTP server that accepts multipart uploads from the broker
(the only thing on the VNet allowed to reach this port) and writes them into
~/uploads/ inside the kiosk container so Chromium's <input type="file">
picker shows them when the SaaS asks for an attachment.

Hardening:
- Binds 0.0.0.0:6902 because the broker reaches us by VNet IP. Subnet NSG
  blocks every other source.
- Filenames are sanitized to a safe basename (no path traversal, no NUL).
- Hard cap on per-request body size (BROKER_INBOX_MAX_BYTES, default 100 MB).
- Optional shared-secret header X-Inbox-Token. Set INBOX_TOKEN env var on
  both the broker and this sandbox to enforce. Empty = no check (relies on
  network isolation only).
- Each upload is logged to stdout with sha256 + size for the audit trail.
"""
from __future__ import annotations

import cgi
import hashlib
import logging
import os
import re
import sys
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("INBOX_PORT", "6902"))
UPLOAD_DIR = Path(os.environ.get("INBOX_DIR", str(Path.home() / "uploads")))
MAX_BYTES = int(os.environ.get("BROKER_INBOX_MAX_BYTES", str(100 * 1024 * 1024)))
TOKEN = os.environ.get("INBOX_TOKEN", "")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(UPLOAD_DIR, 0o700)

logging.basicConfig(level=logging.INFO, format="[inbox] %(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("inbox")

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")


def _safe_basename(raw: str) -> str:
    """Strip any path component, normalize, default to 'upload.bin'."""
    raw = (raw or "").replace("\x00", "").strip()
    base = os.path.basename(raw) or "upload.bin"
    base = _SAFE_NAME.sub("_", base)
    if not base or base in {".", ".."}:
        base = "upload.bin"
    # Avoid hidden files
    if base.startswith("."):
        base = "f" + base
    return base[:200]


class Handler(BaseHTTPRequestHandler):
    server_version = "cloak-inbox/1"
    sys_version = ""

    def log_message(self, fmt: str, *args) -> None:  # noqa: D401, A003
        log.info("%s - %s", self.client_address[0], fmt % args)

    def _send_json(self, status: int, body: dict) -> None:
        import json

        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            return self._send_json(HTTPStatus.OK, {"ok": True})
        if self.path == "/list":
            files = []
            for f in sorted(UPLOAD_DIR.iterdir()):
                if f.is_file():
                    files.append({"name": f.name, "size": f.stat().st_size})
            return self._send_json(HTTPStatus.OK, {"files": files})
        return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/inbox":
            return self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        if TOKEN and self.headers.get("X-Inbox-Token", "") != TOKEN:
            return self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "bad token"})

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "bad length"})
        if length <= 0 or length > MAX_BYTES:
            return self._send_json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                {"error": f"size {length} > cap {MAX_BYTES}"},
            )

        ctype = self.headers.get("Content-Type", "")
        if not ctype.lower().startswith("multipart/form-data"):
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "expected multipart/form-data"})

        # Parse the multipart body. cgi is deprecated but ships with stdlib;
        # the alternative (python-multipart) would add a dep we don't need.
        env = {"REQUEST_METHOD": "POST", "CONTENT_TYPE": ctype, "CONTENT_LENGTH": str(length)}
        try:
            form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ=env, keep_blank_values=True)
        except Exception as ex:  # noqa: BLE001
            log.warning("multipart parse failed: %s", ex)
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "parse failed"})

        if "file" not in form:
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "missing 'file' field"})

        item = form["file"]
        if not getattr(item, "filename", None):
            return self._send_json(HTTPStatus.BAD_REQUEST, {"error": "no filename"})

        name = _safe_basename(item.filename)
        target = UPLOAD_DIR / name
        # If a file by this name already exists, append a numeric suffix.
        if target.exists():
            stem, dot, ext = name.rpartition(".")
            base = stem if dot else name
            tail = ("." + ext) if dot else ""
            n = 1
            while True:
                cand = UPLOAD_DIR / f"{base}-{n}{tail}"
                if not cand.exists():
                    target = cand
                    break
                n += 1

        # Stream-copy with a hash so the broker can record sha256 in the audit log.
        h = hashlib.sha256()
        written = 0
        # cgi has already written to a spooled tempfile; copy to final path.
        try:
            with tempfile.NamedTemporaryFile(dir=str(UPLOAD_DIR), delete=False) as tmp:
                while True:
                    chunk = item.file.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_BYTES:
                        tmp.close()
                        os.unlink(tmp.name)
                        return self._send_json(
                            HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            {"error": "exceeded MAX_BYTES mid-stream"},
                        )
                    h.update(chunk)
                    tmp.write(chunk)
                tmp_path = tmp.name
            os.replace(tmp_path, target)
            os.chmod(target, 0o600)
        except Exception as ex:  # noqa: BLE001
            log.exception("write failed: %s", ex)
            return self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "write failed"})

        sha = h.hexdigest()
        log.info("stored %s size=%d sha256=%s", target.name, written, sha)
        return self._send_json(
            HTTPStatus.CREATED,
            {"name": target.name, "size": written, "sha256": sha, "path": str(target)},
        )


def main() -> int:
    log.info("inbox listening on 0.0.0.0:%d, dir=%s, max=%d", PORT, UPLOAD_DIR, MAX_BYTES)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
