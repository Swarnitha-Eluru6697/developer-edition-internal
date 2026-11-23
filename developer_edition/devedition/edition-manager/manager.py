"""Developer Edition: installs only the edition the user chose when creating their account.

`docker compose up -d --build` starts only the gateway (sign-up / sign-in screens and dashboard) and this service.
Everything else, Postgres, MinIO and Redis included, belongs to the Data or Agent & Model Compose profile
(docker-compose.yml), which nothing starts by default. When the user
picks an edition on the sign-up screen (or after signing in to an install that has none yet), the dashboard asks
this service to install it: it builds and starts that profile's services through the host's Docker and stops the
services of an edition that was not chosen. The choice is kept in /state/edition.json and re-applied on start.

API (reached only through the gateway, which listens on 127.0.0.1):
  GET  /edition      {"editions": [...], "status": "none|installing|ready|failed", "message", "log": [...]}
  POST /edition      {"editions": ["data"] | ["agent"] | ["data", "agent"]}  -> 202, installs in the background
  GET  /edition.js   window.DE_EDITION = "<editions when ready>";  (loaded by the dashboard before its script)
  POST /edition/stop   on sign-out: stops every edition service (the choice is kept)       -> 202 (200 if nothing runs)
  POST /edition/start  on sign-in: starts the chosen editions again after a stop (no rebuild) -> 202 (200 if running)

Status: none | installing | ready | failed | stopping | stopped. While signed out only the gateway and this service
run; signing in starts the chosen editions again.
"""
import json
import os
import signal
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT_DIR = os.environ["DE_PROJECT_DIR"]
STATE_FILE = "/state/edition.json"
EDITIONS = ("data", "agent")
NAMES = {"data": "Data Developer Edition", "agent": "Agent & Model Developer Edition"}
LOG_LINES = 60

_lock = threading.Lock()
_state = {"editions": [], "status": "none", "message": "", "log": [], "updated_at": 0}
_pending = {"op": None}   # "stop" or "start" asked for while another install / stop was running


def compose(*args):
    """docker compose for this project, with no profile selected unless asked for."""
    env = dict(os.environ, COMPOSE_PROFILES="")
    return ["docker", "compose", "--project-directory", PROJECT_DIR, "-f", f"{PROJECT_DIR}/docker-compose.yml", *args], env


def services(*profiles):
    cmd, env = compose(*[a for p in profiles for a in ("--profile", p)], "config", "--services")
    return set(subprocess.run(cmd, env=env, capture_output=True, text=True, check=True).stdout.split())


def edition_services(edition):
    """The services an edition adds to the core (its profile minus everything that runs without one)."""
    return services(edition) - services()


def save():
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({k: _state[k] for k in ("editions", "status", "message", "updated_at")}, f)
    os.replace(tmp, STATE_FILE)


def set_state(**kw):
    with _lock:
        _state.update(kw, updated_at=time.time())
        save()


def log(line):
    with _lock:
        _state["log"] = (_state["log"] + [line.rstrip()])[-LOG_LINES:]


def run(cmd, env):
    log("$ " + " ".join(cmd[cmd.index("compose"):]))
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    for line in p.stdout:
        if line.strip():
            log(line)
    return p.wait()


def install(editions, build=True):
    """Build and start the chosen editions' services; stop the services of the editions not chosen."""
    try:
        wanted = sorted(set().union(*[edition_services(e) for e in editions]))
        others = sorted(set().union(*[edition_services(e) for e in EDITIONS if e not in editions]) - set(wanted))
        set_state(editions=editions, status="installing",
                  message=f"{'Installing' if build else 'Starting'} {' and '.join(NAMES[e] for e in editions)}: {len(wanted)} services")
        if others:
            cmd, env = compose(*[a for e in EDITIONS for a in ("--profile", e)], "stop", *others)
            run(cmd, env)
        cmd, env = compose(*[a for e in editions for a in ("--profile", e)], "up", "-d", *(["--build"] if build else []), *wanted)
        code = run(cmd, env)
        if code != 0:
            set_state(status="failed", message=f"Installing failed (docker compose exited {code}); see the log")
            return
        if "data" in editions:
            reload_gateway_token()
        set_state(status="ready", message=f"{' and '.join(NAMES[e] for e in editions)} {'installed' if build else 'started'}")
    except Exception as e:  # noqa: BLE001 - reported to the dashboard, never raised into the server
        log(f"error: {e}")
        set_state(status="failed", message=f"Installing failed: {e}")
    run_pending()


def stop_all():
    """Signed out: stop every Data and Agent & Model service (Postgres and MinIO included). Containers and data are
    kept, so signing in again only starts them."""
    try:
        every = sorted(set().union(*[edition_services(e) for e in EDITIONS]))
        cmd, env = compose(*[a for e in EDITIONS for a in ("--profile", e)], "stop", *every)
        code = run(cmd, env)
        set_state(status="stopped", message="Signed out: services stopped" if code == 0
                  else f"Stopping exited {code}; see the log")
    except Exception as e:  # noqa: BLE001
        log(f"error: {e}")
        set_state(status="stopped", message=f"Stopping failed: {e}")
    run_pending()


def run_pending():
    with _lock:
        op, _pending["op"] = _pending["op"], None
    if op == "stop":
        request_stop()
    elif op == "start":
        request_start()


def request_stop():
    """202 when stopping (now or after the running install), 200 when nothing runs."""
    with _lock:
        st = _state["status"]
        if st in ("installing", "stopping"):
            _pending["op"] = "stop" if st == "installing" else None
            return 202
        if st != "ready" or not _state["editions"]:
            return 200
        _state["status"] = "stopping"
    set_state(message="Stopping services (signed out)")
    threading.Thread(target=stop_all, daemon=True).start()
    return 202


def request_start():
    """202 when starting (now or after the running stop), 200 when already running or nothing was chosen."""
    with _lock:
        st, eds = _state["status"], list(_state["editions"])
        if st == "stopping":
            _pending["op"] = "start"
            return 202
        if st == "installing":
            _pending["op"] = None if _pending["op"] == "stop" else _pending["op"]
            return 202
        if st != "stopped" or not eds:
            return 200
    set_state(message=f"Starting {' and '.join(NAMES[e] for e in eds)}")
    start_install(eds, build=False)
    return 202


def reload_gateway_token():
    """The gateway reads the Data Quality bearer (minted by jwks, a Data service) when it starts. The gateway runs
    before any edition, so after jwks comes up restart it once if its config does not carry the current token."""
    cmd, env = compose("exec", "-T", "gateway", "sh", "-c",
                       '[ ! -s /jwks/token.txt ] || grep -qF "$(tr -d "\\n\\r" < /jwks/token.txt)" /etc/nginx/conf.d/default.conf')
    if subprocess.run(cmd, env=env, capture_output=True).returncode != 0:
        cmd, env = compose("restart", "gateway")
        run(cmd, env)


def start_install(editions, build=True):
    with _lock:
        if _state["status"] == "installing":
            return False
        _state["status"] = "installing"
    threading.Thread(target=install, args=(editions, build), daemon=True).start()
    return True


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        with _lock:
            snap = dict(_state, log=list(_state["log"]))
        if self.path == "/edition":
            return self._send(200, snap)
        if self.path == "/edition.js":
            eds = ",".join(snap["editions"]) if snap["status"] == "ready" else ""
            return self._send(200, f"window.DE_EDITION = {json.dumps(eds)};\n", "application/javascript")
        if self.path == "/health":
            return self._send(200, {"ok": True})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path in ("/edition/stop", "/edition/start"):
            code = request_stop() if self.path == "/edition/stop" else request_start()
            with _lock:
                return self._send(code, {"editions": _state["editions"], "status": _state["status"]})
        if self.path != "/edition":
            return self._send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            editions = sorted({str(e) for e in body.get("editions", [])})
        except Exception:  # noqa: BLE001
            return self._send(400, {"error": "body must be JSON: {\"editions\": [\"data\" | \"agent\", ...]}"})
        if not editions or any(e not in EDITIONS for e in editions):
            return self._send(400, {"error": "editions must be one or both of: data, agent"})
        if not start_install(editions):
            return self._send(409, {"error": "an installation is already running"})
        self._send(202, {"editions": editions, "status": "installing"})

    def log_message(self, fmt, *args):  # quiet access log
        pass


def main():
    try:
        with open(STATE_FILE) as f:
            _state.update(json.load(f))
    except FileNotFoundError:
        pass
    # An installation cut short (container restarted) or a chosen edition whose services were stopped by a plain
    # `docker compose up -d`: bring the chosen editions up again (no rebuild).
    # Signed out (stopped, or cut short while stopping): stay stopped until the next sign-in.
    if _state["status"] == "stopping":
        _state["status"] = "stopped"
    if _state["editions"] and _state["status"] in ("ready", "installing"):
        _state["status"] = "ready"
        start_install(list(_state["editions"]), build=False)
    # Exit at once on `docker compose stop` (PID 1 ignores SIGTERM unless it has a handler).
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    ThreadingHTTPServer(("0.0.0.0", 9100), Handler).serve_forever()


if __name__ == "__main__":
    main()
