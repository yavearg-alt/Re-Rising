"""SAO Unleash Blading private server.

Routes (the patched APK points every URL at this server):
  /cdn/...          original asset CDN (manifests, hashed files, bulk zips)
  /gcs/<region>/... maintenance.json + announcement pages bucket
  everything else   game API
"""
import json
import os
import sys
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import admin  # noqa: E402
import botapi  # noqa: E402
import idspage  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
from api import handle_api, seed_platforms_from_logs  # noqa: E402
from assets import AssetStore  # noqa: E402
from gree import handle_gree  # noqa: E402

STORE = AssetStore()
os.makedirs(config.LOG_DIR, exist_ok=True)


def log(msg: str):
    line = time.strftime("%H:%M:%S ") + msg
    print(line, flush=True)
    with open(config.log_file("server.log"), "a", encoding="utf-8") as f:
        f.write(line + "\n")


BOT_HITS = 0
SEEN_PATHS = set()  # non-game paths already logged once (a WebView page of the game would show up here)
MAX_BODY = 4 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # headers and body go out in two writes: with Nagle on, each small file waited one extra round trip
    disable_nagle_algorithm = True
    server_version, sys_version = "SAOUB", ""  # no software versions in the Server header
    # idle connections (slowloris) are dropped instead of holding a thread forever; 5 min so that a slow or paused
    # mobile download is not cut (60 s cut real downloads: "Connection timed out" in sendfile)
    timeout = 300

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("X-SAOUB-Server", config.BRAND)
        if config.INSTANCE:
            self.send_header("X-SAOUB-Instance", config.INSTANCE)
        super().end_headers()

    def _send(self, code: int, body: bytes, ctype: str = "application/octet-stream", headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_file(self, path: str, file: str):
        """Asset file straight from disk (os.sendfile): nothing loaded in the process memory."""
        size = os.path.getsize(file)
        log(f"CDN 200 {path} ({size} B)")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        if self.command != "HEAD":
            with open(file, "rb") as f:
                self.connection.sendfile(f)

    def _handle(self):
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            # the game's largest request (user/push) is ~80 KB: refuse instead of reading anything into memory
            self.close_connection = True
            return self._send(413, b"request too large", "text/plain")
        body = self.rfile.read(length) if length else b""
        try:
            # asset files can be asked on the CDN prefix or directly on the API host (version manifest)
            if path.startswith("/cdn/") or path.endswith(".manifestenc") or "/hashed/" in path or path.endswith(".zip"):
                file = STORE.resolve_file(path)
                if file is not None:
                    return self._send_file(path, file)
                res = STORE.resolve(path)
                if res is None:
                    log(f"CDN 404 {path}")
                    return self._send(404, b"not found", "text/plain")
                log(f"CDN 200 {path} ({len(res[0])} B)")
                return self._send(200, res[0], res[1])
            if path.startswith("/gcs/"):
                if path.endswith("maintenance.json"):
                    # same shape as the real file; maintenance is switched on from the admin panel
                    data = admin.maintenance_json()
                    log(f"GCS {path}")
                    return self._send(200, json.dumps(data).encode(), "application/json")
                log(f"GCS 404 {path}")
                return self._send(404, b"not found", "text/plain")
            if path.startswith("/gree/"):
                code, resp_body, ctype, headers = handle_gree(
                    self.command, self.path, {k.upper(): v for k, v in self.headers.items()}, body, log,
                    self.client_address[0])
                return self._send(code, resp_body, ctype, headers)
            if path.startswith("/v1.0/"):
                # iOS GREE SDK: it keeps only the host and scheme of its server URL (no port, no /gree), so it
                # calls https://<VPS name>/v1.0/..., forwarded here by Caddy. Same routes as /gree/v1.0/...; the
                # signature uses the URL the client called, the address limits the client's real IP.
                hdrs = {k.upper(): v for k, v in self.headers.items()}
                via_proxy = self.client_address[0].startswith(("172.", "10.", "192.168.", "127."))
                forwarded = hdrs.get("X-FORWARDED-FOR", "").split(",")[0].strip() if via_proxy else ""
                ip = forwarded or self.client_address[0]
                proto = hdrs.get("X-FORWARDED-PROTO", "http") if via_proxy else "http"
                host = (hdrs.get("X-FORWARDED-HOST") if via_proxy else None) or hdrs.get("HOST", "")
                code, resp_body, ctype, headers = handle_gree(
                    self.command, "/gree" + self.path, hdrs, body, log, ip, public_url=f"{proto}://{host}{self.path}")
                return self._send(code, resp_body, ctype, headers)
            if path == "/announcement" or path.startswith("/announcement/"):
                # news pages, opened by the game in a WebView (no SAOAL- headers); written from the admin panel
                return self._send(200, admin.game_page(path), "text/html; charset=utf-8")
            if path == "/ids" and self.command in ("GET", "HEAD"):
                # public list of character / item / equipment ids for the Discord bot's /claim (game data only)
                return self._send(200, idspage.page(), "text/html; charset=utf-8",
                                  {"Cache-Control": "public, max-age=3600", "X-Content-Type-Options": "nosniff",
                                   "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                                                              "script-src 'unsafe-inline'; frame-ancestors 'none'"})
            if not any(k.upper().startswith("SAOAL-") for k in self.headers.keys()):
                # not the game (internet scanners probing /owa/, /webui, /api/v2/about...): drop quietly
                global BOT_HITS
                BOT_HITS += 1
                if len(SEEN_PATHS) < 2000 and path[:80] not in SEEN_PATHS:
                    # every distinct path once: pages the game opens in a WebView (scout rates...) come this way
                    SEEN_PATHS.add(path[:80])
                    log(f"ignored non-game request {self.command} {path[:80]} (UA {self.headers.get('User-Agent', '')[:60]})")
                elif BOT_HITS % 100 == 1:
                    log(f"ignored {BOT_HITS} non-game request(s), last: {self.command} {path[:80]}")
                return self._send(404, b"not found", "text/plain")
            code, resp_body, ctype, headers = handle_api(self.command, path, dict(self.headers), body, log)
            return self._send(code, resp_body, ctype, headers)
        except Exception:
            log("ERROR " + traceback.format_exc())
            return self._send(500, b"internal error", "text/plain")

    do_GET = do_POST = do_PUT = do_HEAD = _handle


class Server(ThreadingHTTPServer):
    # The client opens ~20 parallel connections per download batch; with the default listen backlog (5) the
    # extra ones are refused before reaching us -> "Unable to download file" (error 7) with nothing logged.
    request_queue_size = 1024
    daemon_threads = True


def welcome_announcement():
    """A new database (local launcher) starts with an announcement saying whose server this is. Once."""
    if db.get_setting("welcome_announcement") or db.list_announcements():
        return
    db.save_announcement(None, "Welcome", f"**{config.BRAND}**\n\nThis server software is the work of the "
                         "Re:Rising project (open source), an unofficial fan project not affiliated with Bandai "
                         "Namco.\n\nHave fun, and send your bug reports with your player ID, "
                         "the time and a screenshot!\n", "", True)
    db.set_setting("welcome_announcement", 1)


def run():
    admin.apply_settings()
    seed_platforms_from_logs(log)
    welcome_announcement()
    if admin.PASSWORD:
        admin.serve()
        log(f"admin panel on port {admin.PORT}, path {admin.BASE}/")
    if botapi.SECRET:
        # Discord bot API: internal only (Docker network), never published nor proxied
        botapi.serve(log)
        log(f"discord bot API on {botapi.HOST}:{botapi.PORT}")
    log(f"asset version {STORE.version}, {len(STORE.project['assets'])} manifest entries")
    log(f"listening on {config.HOST}:{config.PORT}")
    Server((config.HOST, config.PORT), Handler).serve_forever()


if __name__ == "__main__":
    run()
