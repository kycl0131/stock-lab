"""Local, read-only dashboard; no credentials or order endpoints are exposed."""
import json
import base64
import hashlib
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .store import Store


def serve(store: Store, port=8765):
    html = Path(__file__).with_name("web").joinpath("index.html").read_bytes()
    scripts = re.findall(rb"<script>(.*?)</script>", html, re.S)
    script_hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(s.replace(b'\r\n', b'\n')).digest()).decode() + "'" for s in scripts)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if path == "/":
                body, content_type = html, "text/html; charset=utf-8"
            elif path == "/api/state":
                body, content_type = json.dumps(store.state(), ensure_ascii=False).encode(), "application/json; charset=utf-8"
            elif path == "/health":
                body, content_type = b'{"status":"ok","execution":"PAPER_ONLY"}', "application/json"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", f"default-src 'self'; script-src {script_hashes}; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Stock Lab: http://127.0.0.1:{port} (read-only, paper only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
