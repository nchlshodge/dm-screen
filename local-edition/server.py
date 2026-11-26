#!/usr/bin/env python3
"""
DM Screen - Local Edition
Runs the DM Screen from your own computer so players can join on your Wi-Fi with a 4-digit code.
Needs only Python 3.8+ (nothing to install). Start it, then your browser opens the DM Screen.

  python server.py            (Windows: double-click "Start DM Screen (Windows).bat")
  python server.py --port 8090

Your game is saved in the data folder next to this file.
"""
import argparse, json, mimetypes, os, queue, random, re, socket, sys, threading, time, uuid, webbrowser
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
UPLOADS = os.path.join(DATA_DIR, "uploads")
GAME = os.path.join(DATA_DIR, "game.json")
SEED = os.path.join(HERE, "seed.json")
MAX_UPLOAD = 25 * 1024 * 1024

lock = threading.RLock()
docs = {}                 # path -> document (same layout as the online DM Screen)
dm_streams = []           # queues for DM screen + table windows on this computer
players = {}              # pid -> {name, char, token, streams, last}
tokens = {}               # token -> pid
messages = []             # chat + handouts this session
code = "%04d" % random.randint(0, 9999)
player_url = ""
_save_timer = None


# ---------------------------------------------------------------- storage
def load():
    global docs
    os.makedirs(UPLOADS, exist_ok=True)
    src = GAME if os.path.exists(GAME) else SEED
    try:
        with open(src, encoding="utf-8") as f:
            docs = json.load(f).get("docs", {})
    except Exception as e:
        print("Could not read saved game (%s). Starting fresh." % e)
        docs = {}


def save_soon():
    global _save_timer
    with lock:
        if _save_timer:
            _save_timer.cancel()
        _save_timer = threading.Timer(0.8, save_now)
        _save_timer.daemon = True
        _save_timer.start()


def save_now():
    with lock:
        tmp = GAME + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"docs": docs}, f)
        os.replace(tmp, GAME)


# ---------------------------------------------------------------- broadcasting
def to_dm(msg):
    data = json.dumps(msg)
    with lock:
        for q in list(dm_streams):
            q.put(data)


def to_player(pid, msg):
    data = json.dumps(msg)
    with lock:
        p = players.get(pid)
        if p:
            for q in list(p["streams"]):
                q.put(data)


def player_list():
    with lock:
        out = []
        for pid, p in players.items():
            h = docs.get("heroes/%s" % p["char"]) if p["char"] else None
            out.append({"pid": pid, "name": p["name"], "char": p["char"], "charName": (h or {}).get("name", ""),
                        "online": bool(p["streams"]), "joined": p["joined"]})
        return sorted(out, key=lambda x: x["joined"])


def hero_list():
    with lock:
        return sorted([{"id": k.split("/", 1)[1], "name": v.get("name") or "Unnamed"}
                       for k, v in docs.items() if k.startswith("heroes/")], key=lambda x: x["name"].lower())


def msgs_for(pid):
    return [m for m in messages if m["to"] in (pid, "all") or m["from"] == pid]


def add_message(frm, to, text="", handout=None):
    m = {"id": uuid.uuid4().hex[:10], "t": int(time.time() * 1000), "from": frm, "to": to,
         "text": str(text or "")[:2000], "handout": handout}
    with lock:
        messages.append(m)
        del messages[:-500]
    to_dm({"type": "chat", "msg": m})
    if to == "all":
        for pid in list(players):
            to_player(pid, {"type": "msg", "msg": m})
    elif to != "dm":
        to_player(to, {"type": "msg", "msg": m})
    if frm not in ("dm",):
        to_player(frm, {"type": "msg", "msg": m})
    return m


def lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return "127.0.0.1"


# ---------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "DMScreen/1.0"

    def log_message(self, fmt, *args):
        pass  # keep the window quiet

    # helpers
    def is_local(self):
        return self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")

    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype=None):
        if not os.path.isfile(path):
            return self.send_error(404)
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype or mimetypes.guess_type(path)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2 * 1024 * 1024:
            raise ValueError("too big")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def stream(self, q, first=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            if first:
                self.wfile.write(("data: %s\n\n" % json.dumps(first)).encode())
                self.wfile.flush()
            while True:
                try:
                    data = q.get(timeout=15)
                    self.wfile.write(("data: %s\n\n" % data).encode())
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def player(self, token):
        with lock:
            pid = tokens.get(token or "")
            return (pid, players.get(pid)) if pid else (None, None)

    # routes
    def do_GET(self):
        u = urlparse(self.path)
        path, qs = u.path, parse_qs(u.query)
        if path in ("/", "/index.html"):
            if not self.is_local():
                self.send_response(302)
                self.send_header("Location", "/play")
                self.end_headers()
                return
            return self.send_file(os.path.join(HERE, "dm.html"), "text/html; charset=utf-8")
        if path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/play":
            return self.send_file(os.path.join(HERE, "player.html"), "text/html; charset=utf-8")
        if path.startswith("/_blob/"):
            name = os.path.basename(unquote(path[7:]))
            return self.send_file(os.path.join(UPLOADS, name))
        if path == "/api/ping":
            return self.send_json({"ok": True, "local": self.is_local()})
        if path == "/api/state":
            if not self.is_local():
                return self.send_error(403)
            with lock:
                return self.send_json({"docs": docs, "code": code, "url": player_url,
                                       "players": player_list(), "chat": messages[-200:]})
        if path == "/api/events":
            if not self.is_local():
                return self.send_error(403)
            q = queue.Queue()
            with lock:
                dm_streams.append(q)
            try:
                self.stream(q)
            finally:
                with lock:
                    if q in dm_streams:
                        dm_streams.remove(q)
            return
        if path == "/api/pcheck":
            pid, p = self.player((qs.get("token") or [""])[0])
            return self.send_json({"ok": bool(p)}, 200 if p else 401)
        if path == "/api/pevents":
            pid, p = self.player((qs.get("token") or [""])[0])
            if not p:
                return self.send_error(401)
            q = queue.Queue()
            with lock:
                p["streams"].append(q)
                hero = docs.get("heroes/%s" % p["char"]) if p["char"] else None
                hello = {"type": "hello", "name": p["name"], "char": p["char"], "hero": hero,
                         "heroes": hero_list(), "messages": msgs_for(pid)}
            to_dm({"type": "players", "players": player_list()})
            try:
                self.stream(q, hello)
            finally:
                with lock:
                    if q in p["streams"]:
                        p["streams"].remove(q)
                to_dm({"type": "players", "players": player_list()})
            return
        self.send_error(404)

    def do_POST(self):
        global code
        path = urlparse(self.path).path
        try:
            if path == "/api/upload":
                if not self.is_local():
                    return self.send_error(403)
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > MAX_UPLOAD:
                    return self.send_json({"error": "File too big (25 MB max)"}, 413)
                ctype = self.headers.get("Content-Type", "")
                ext = mimetypes.guess_extension(ctype.split(";")[0].strip()) or ""
                if not ext:
                    ext = os.path.splitext(unquote(self.headers.get("X-Filename", "")))[1][:8]
                ext = re.sub(r"[^.a-zA-Z0-9]", "", ext)
                fid = uuid.uuid4().hex + ext
                with open(os.path.join(UPLOADS, fid), "wb") as f:
                    f.write(self.rfile.read(n))
                return self.send_json({"id": fid, "url": "/_blob/" + fid})

            b = self.body_json()

            # ---- DM (this computer only)
            if path.startswith("/api/") and path in ("/api/set", "/api/emit", "/api/newcode", "/api/tell", "/api/kick"):
                if not self.is_local():
                    return self.send_error(403)
                if path == "/api/set":
                    p = str(b.get("path", ""))
                    if not re.fullmatch(r"[A-Za-z0-9_.~:@+-]+(/[A-Za-z0-9_.~:@+-]+)+", p):
                        return self.send_json({"error": "bad path"}, 400)
                    data = b.get("data")
                    with lock:
                        if data is None:
                            docs.pop(p, None)
                        else:
                            docs[p] = data
                    save_soon()
                    to_dm({"type": "doc", "path": p, "data": data})
                    if p.startswith("heroes/"):
                        hid = p.split("/", 1)[1]
                        for pid, pl in list(players.items()):
                            if pl["char"] == hid:
                                to_player(pid, {"type": "hero", "hero": data})
                            to_player(pid, {"type": "heroes", "heroes": hero_list()})
                    return self.send_json({"ok": True})
                if path == "/api/emit":
                    to_dm({"type": "room", "topic": str(b.get("topic", ""))[:48], "data": b.get("data"), "tab": b.get("tab")})
                    return self.send_json({"ok": True})
                if path == "/api/newcode":
                    code = "%04d" % random.randint(0, 9999)
                    to_dm({"type": "code", "code": code})
                    return self.send_json({"code": code})
                if path == "/api/tell":
                    to = b.get("to") or "all"
                    if to != "all" and to not in players:
                        return self.send_json({"error": "That player left"}, 404)
                    h = b.get("handout")
                    if h:
                        h = {"title": str(h.get("title") or "")[:200], "text": str(h.get("text") or "")[:5000],
                             "img": str(h.get("img") or "")[:200] or None}
                    m = add_message("dm", to, b.get("text", ""), h)
                    return self.send_json({"ok": True, "msg": m})
                if path == "/api/kick":
                    pid = b.get("pid")
                    with lock:
                        p = players.pop(pid, None)
                        if p:
                            tokens.pop(p["token"], None)
                    if p:
                        for q in p["streams"]:
                            q.put(json.dumps({"type": "kicked"}))
                    to_dm({"type": "players", "players": player_list()})
                    return self.send_json({"ok": True})

            # ---- players
            if path == "/api/join":
                if str(b.get("code", "")).strip() != code:
                    time.sleep(0.4)
                    return self.send_json({"error": "That code doesn't match. Check the DM's screen."}, 403)
                name = re.sub(r"\s+", " ", str(b.get("name", "")).strip())[:24] or "Player"
                pid, token = uuid.uuid4().hex[:8], uuid.uuid4().hex
                with lock:
                    players[pid] = {"name": name, "char": None, "token": token, "streams": [], "joined": time.time()}
                    tokens[token] = pid
                to_dm({"type": "players", "players": player_list()})
                return self.send_json({"token": token, "pid": pid, "name": name})
            pid, p = self.player(b.get("token"))
            if not p:
                return self.send_json({"error": "Please join again."}, 401)
            if path == "/api/pchar":
                hid = b.get("char")
                with lock:
                    if hid and "heroes/%s" % hid not in docs:
                        return self.send_json({"error": "No such character"}, 404)
                    p["char"] = hid or None
                    hero = docs.get("heroes/%s" % hid) if hid else None
                to_player(pid, {"type": "hero", "hero": hero, "char": p["char"]})
                to_dm({"type": "players", "players": player_list()})
                return self.send_json({"ok": True})
            if path == "/api/psay":
                text = str(b.get("text", "")).strip()[:1000]
                if text:
                    add_message(pid, "dm", text)
                return self.send_json({"ok": True})
            if path == "/api/proll":
                rec = b.get("roll") or {}
                hero = docs.get("heroes/%s" % p["char"], {}) if p["char"] else {}
                clean = {"id": uuid.uuid4().hex[:10], "t": int(time.time() * 1000),
                         "who": (hero.get("name") or p["name"])[:40], "label": str(rec.get("label", ""))[:40],
                         "expr": str(rec.get("expr", ""))[:40], "total": rec.get("total"), "detail": str(rec.get("detail", ""))[:300],
                         "nat": rec.get("nat"), "secret": bool(rec.get("secret"))}
                to_dm({"type": "proll", "roll": clean})
                return self.send_json({"ok": True})
        except (ValueError, json.JSONDecodeError):
            return self.send_json({"error": "bad request"}, 400)
        self.send_error(404)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    global player_url
    ap = argparse.ArgumentParser(description="DM Screen - Local Edition")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    load()
    srv = None
    for port in range(a.port, a.port + 12):
        try:
            srv = Server(("0.0.0.0", port), Handler)
            break
        except OSError:
            continue
    if not srv:
        sys.exit("Could not find a free port. Close other programs or use --port.")
    port = srv.server_address[1]
    player_url = "http://%s:%d/play" % (lan_ip(), port)
    print("\n  DM Screen (Local Edition) is running.\n")
    print("  Your screen:      http://localhost:%d" % port)
    print("  Table screen:     http://localhost:%d/#table" % port)
    print("  Players go to:    %s" % player_url)
    print("  Join code:        %s   (shown on the Players tab too)\n" % code)
    print("  Keep this window open while you play. Close it to stop.\n")
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open("http://localhost:%d" % port)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        save_now()
        print("Saved. Bye!")


if __name__ == "__main__":
    main()
