
"""Local editor for catalog evidence and decisions."""
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import secrets
from urllib.parse import urlsplit, unquote

from catalog_pipeline import connect, review, dump

PAGE = Path(__file__).with_name("catalog_review.html")


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, value, mime="application/json; charset=utf-8"):
        payload = (dump(value) if mime.startswith("application/json") else value).encode()
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "img-src https://upload.wikimedia.org https://thumb.wikimedia.org; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(payload)

    def allowed(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if not self.allowed():
            return self.reply(403, {"error":"Use the printed localhost URL"})
        path = urlsplit(self.path).path
        if path == "/":
            return self.reply(200, PAGE.read_text(encoding="utf-8-sig").replace("__LOCAL_TOKEN__",self.server.token),
                              "text/html; charset=utf-8")
        with closing(connect(self.server.db)) as db:
            if path == "/queue":
                rows = db.execute("""SELECT id, state, json_extract(data,'$.name') name,
                    json_array_length(json_extract(data,'$.findings')) priority FROM places
                    WHERE data IS NOT NULL AND (state IN ('needs_review','checking','stale')
                    OR (state IN ('ready','published') AND json_extract(data,'$.signals.photos')='review'))
                    ORDER BY CASE state WHEN 'needs_review' THEN 0 WHEN 'ready' THEN 1 ELSE 2 END, priority DESC,id""")
                return self.reply(200,[dict(row) for row in rows])
            if path.startswith("/place/"):
                pid = unquote(path.removeprefix("/place/"))
                row = db.execute("SELECT * FROM places WHERE id=?", (pid,)).fetchone()
                if row:
                    info = dict(row)
                    for key in ("data","raw","initial","decision"):
                        info[key]=json.loads(info[key]) if info[key] else None
                    info["facts"]=[{**dict(r),"value":json.loads(r["value"])} for r in db.execute(
                        "SELECT * FROM facts WHERE place=? ORDER BY field,source",(pid,))]
                    info["events"]=[dict(r) for r in db.execute("SELECT state,reason,at FROM events WHERE place=? ORDER BY rowid DESC LIMIT 15",(pid,))]
                    return self.reply(200,info)
        self.reply(404,{"error":"Not found"})

    def do_POST(self):
        origin=f"http://127.0.0.1:{self.server.server_port}"
        if (not self.allowed() or self.headers.get("Origin")!=origin
                or self.headers.get("X-Review-Token")!=self.server.token
                or self.headers.get_content_type()!="application/json"
                or self.path!="/decision"):
            return self.reply(403,{"error":"Local editor only"})
        try:
            length=int(self.headers.get("Content-Length","0"))
            if not 0<length<=32768:
                raise ValueError("Request too large")
            payload=json.loads(self.rfile.read(length))
            with closing(connect(self.server.db)) as db:
                review(db,payload["id"],payload["action"],payload["change"])
        except (ValueError,KeyError,TypeError) as exc:
            return self.reply(400,{"error":str(exc)})
        self.reply(200,{"ok":True})


def serve(path, port):
    with ThreadingHTTPServer(("127.0.0.1",port),Handler) as server:
        server.db=path
        server.token=secrets.token_urlsafe(32)
        print(f"http://127.0.0.1:{server.server_port}/",flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass

