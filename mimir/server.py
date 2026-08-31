"""Local HTTP daemon for one Mimir instance.

Stdlib only, deliberately boring JSON-in/JSON-out — so Kotlin (Mk2), Python
(Feneris, Skjoldr's Linux side), and PowerShell (Skjoldr's Windows side) can
all call it the same way. See SPEC.md §5.

Run: python -m mimir.server --db path/to/instance.db --port 8420
"""

import argparse
import json
import sqlite3
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import schema, storage


class MimirHandler(BaseHTTPRequestHandler):
    conn: sqlite3.Connection  # set per-server via make_handler()

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length) or b"{}")

    def _ok(self, data) -> None:
        self._send(200, {"ok": True, "data": data})

    def _fail(self, status: int, error_code: str, message: str) -> None:
        self._send(status, {"ok": False, "error_code": error_code, "message": message})

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
        try:
            body = self._read_json()
            if self.path == "/recall":
                nodes = storage.recall(self.conn, body.get("query", ""), body.get("layers"))
                self._ok([asdict(n) for n in nodes])
            elif self.path == "/write":
                node = storage.write_node(
                    self.conn,
                    layer=body["layer"], topic=body["topic"], content=body["content"],
                    authored_by=body.get("authored_by", "unknown"),
                    origin_machine=body.get("origin_machine", "unknown"),
                    description=body.get("description", ""), tags=body.get("tags"),
                    authorized_by=body.get("authorized_by"), confirmation=body.get("confirmation", False),
                )
                self._ok(asdict(node))
            elif self.path == "/confirm":
                node = storage.confirm_node(self.conn, body["node_id"], body.get("actor", "unknown"))
                self._ok(asdict(node))
            elif self.path == "/consolidate":
                self._ok(storage.consolidate(self.conn))
            elif self.path == "/resolve_conflict":
                storage.resolve_conflict(self.conn, body["conflict_id"], body["resolution"])
                self._ok({"resolved": True})
            else:
                self._fail(404, "unknown_route", f"no such route: {self.path}")
        except storage.AuthorizationError as e:
            self._fail(403, "authorization_required", str(e))
        except (KeyError, ValueError) as e:
            self._fail(400, "bad_request", str(e))
        except Exception as e:  # noqa: BLE001 — mirrors SkjoldrCLI's top-level catch-all
            self._fail(500, "internal_error", str(e))

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/conflicts":
            self._ok(storage.list_conflicts(self.conn))
        else:
            self._fail(404, "unknown_route", f"no such route: {self.path}")

    def log_message(self, fmt: str, *args) -> None:  # quiet by default
        pass


def make_handler(conn: sqlite3.Connection):
    return type("BoundMimirHandler", (MimirHandler,), {"conn": conn})


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8420) -> None:
    conn = schema.connect(db_path)
    handler = make_handler(conn)
    # Single-threaded, deliberately: one shared sqlite3.Connection isn't safe
    # across threads, and a local per-instance memory daemon has no need for
    # concurrent request handling in the first place. See SPEC.md's "don't
    # add infrastructure a fail-closed system doesn't need."
    httpd = HTTPServer((host, port), handler)
    print(f"[mimir] instance at {db_path} listening on {host}:{port}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to this instance's SQLite database file")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8420)
    args = parser.parse_args()
    serve(args.db, args.host, args.port)


if __name__ == "__main__":
    main()
