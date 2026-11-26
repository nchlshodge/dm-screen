// One Session = one game. A port of local-edition/server.py to a Durable Object:
//   documents, players and chat live in SQLite; live events go over hibernating WebSockets.
// Who may do what:
//   DM   -> holds the secret key (sent as X-DM-Key, or as the "dm.<key>" WebSocket subprotocol)
//   Player -> holds a token from /api/join (body "token", or the "p.<token>" subprotocol)
import { DurableObject } from "cloudflare:workers";
import SEED from "../seed.json";
import { sha256 } from "./util.js";

const MAX_UPLOAD = 10 * 1024 * 1024; // pictures only
const MAX_DOC = 1_500_000; // SQLite rows top out at 2 MB
const MAX_PLAYERS = 12;
const MAX_DOCS = 2000;
const IDLE_MS = 30 * 24 * 3600 * 1000; // a session nobody touches for 30 days is deleted
const PATH_RE = /^[A-Za-z0-9_.~:@+-]+(\/[A-Za-z0-9_.~:@+-]+)+$/;
const WRONG = "That code doesn't match. Check the DM's screen.";

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });
const rid = (n) => [...crypto.getRandomValues(new Uint8Array(n))].map((x) => x.toString(16).padStart(2, "0")).join("");
const same = (a, b) => {
  if (a.length !== b.length) return false;
  let d = 0;
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return d === 0;
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export class Session extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.keyHash = null;
    this.code = null;
    this.lastTouch = 0;
    this.badKeys = [];
    ctx.blockConcurrencyWhile(async () => {
      this.q(`CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)`);
      this.q(`CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, data TEXT NOT NULL)`);
      this.q(`CREATE TABLE IF NOT EXISTS players (pid TEXT PRIMARY KEY, name TEXT, token TEXT UNIQUE, char TEXT, joined INTEGER)`);
      this.q(`CREATE TABLE IF NOT EXISTS messages (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT, t INTEGER, frm TEXT, to_ TEXT, text TEXT, handout TEXT)`);
      this.keyHash = this.meta("keyHash");
      this.code = this.meta("code");
      ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
    });
  }

  // ---------------------------------------------------------------- storage helpers
  q(sql, ...args) {
    return this.sql.exec(sql, ...args); // SQLite, not a shell
  }
  meta(k) {
    const r = this.q(`SELECT v FROM meta WHERE k=?`, k).toArray();
    return r.length ? r[0].v : null;
  }
  setMeta(k, v) {
    this.q(`INSERT INTO meta (k,v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v`, k, v);
  }
  getDoc(path) {
    const r = this.q(`SELECT data FROM docs WHERE path=?`, path).toArray();
    return r.length ? JSON.parse(r[0].data) : null;
  }
  allDocs() {
    const out = {};
    for (const r of this.q(`SELECT path,data FROM docs`)) out[r.path] = JSON.parse(r.data);
    return out;
  }
  async touch() {
    const now = Date.now();
    if (now - this.lastTouch < 3600_000) return;
    this.lastTouch = now;
    await this.ctx.storage.setAlarm(now + IDLE_MS);
  }

  // ---------------------------------------------------------------- broadcasting
  send(ws, msg) {
    try {
      ws.send(JSON.stringify(msg));
    } catch {}
  }
  toDM(msg) {
    for (const ws of this.ctx.getWebSockets("dm")) this.send(ws, msg);
  }
  toPlayer(pid, msg) {
    for (const ws of this.ctx.getWebSockets("p:" + pid)) this.send(ws, msg);
  }
  playerList(except) {
    const out = [];
    for (const p of this.q(`SELECT pid,name,char,joined FROM players ORDER BY joined`)) {
      const h = p.char ? this.getDoc("heroes/" + p.char) : null;
      const online = this.ctx.getWebSockets("p:" + p.pid).some((w) => w !== except);
      out.push({ pid: p.pid, name: p.name, char: p.char, charName: (h || {}).name || "", online, joined: p.joined });
    }
    return out;
  }
  heroList() {
    const out = [];
    for (const r of this.q(`SELECT path,data FROM docs WHERE path LIKE 'heroes/%'`)) {
      out.push({ id: r.path.split("/")[1], name: JSON.parse(r.data).name || "Unnamed" });
    }
    return out.sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase()));
  }
  msgRow(r) {
    return { id: r.id, t: r.t, from: r.frm, to: r.to_, text: r.text, handout: r.handout ? JSON.parse(r.handout) : null };
  }
  msgsFor(pid) {
    return this.q(`SELECT id,t,frm,to_,text,handout FROM messages WHERE to_ IN (?, 'all') OR frm=? ORDER BY seq DESC LIMIT 200`, pid, pid)
      .toArray().reverse().map((r) => this.msgRow(r));
  }
  chatForDM() {
    return this.q(`SELECT id,t,frm,to_,text,handout FROM messages ORDER BY seq DESC LIMIT 200`)
      .toArray().reverse().map((r) => this.msgRow(r));
  }
  addMessage(frm, to, text = "", handout = null) {
    const m = { id: rid(5), t: Date.now(), from: frm, to, text: String(text || "").slice(0, 2000), handout };
    this.q(`INSERT INTO messages (id,t,frm,to_,text,handout) VALUES (?,?,?,?,?,?)`, m.id, m.t, frm, to, m.text, handout ? JSON.stringify(handout) : null);
    this.q(`DELETE FROM messages WHERE seq <= (SELECT MAX(seq) FROM messages) - 500`);
    this.toDM({ type: "chat", msg: m });
    if (to === "all") for (const p of this.q(`SELECT pid FROM players`)) this.toPlayer(p.pid, { type: "msg", msg: m });
    else if (to !== "dm") this.toPlayer(to, { type: "msg", msg: m });
    if (frm !== "dm") this.toPlayer(frm, { type: "msg", msg: m });
    return m;
  }

  // ---------------------------------------------------------------- auth
  async dmOk(key) {
    if (!this.keyHash || !key) return false;
    const now = Date.now();
    this.badKeys = this.badKeys.filter((t) => now - t < 600_000);
    if (this.badKeys.length >= 20) return false; // too many wrong guesses lately
    const ok = same(await sha256(key), this.keyHash);
    if (!ok) this.badKeys.push(now);
    return ok;
  }
  playerByToken(token) {
    if (!token) return null;
    const r = this.q(`SELECT pid,name,char,token FROM players WHERE token=?`, String(token)).toArray();
    return r.length ? r[0] : null;
  }
  protocols(request) {
    return (request.headers.get("Sec-WebSocket-Protocol") || "").split(",").map((s) => s.trim()).filter(Boolean);
  }

  // ---------------------------------------------------------------- http
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/init" && request.method === "POST") {
      if (this.keyHash) return json({ error: "taken" }, 409);
      const b = await request.json();
      this.keyHash = String(b.keyHash);
      this.code = String(b.code);
      this.setMeta("keyHash", this.keyHash);
      this.setMeta("code", this.code);
      this.setMeta("created", String(Date.now()));
      this.setMeta("title", String(b.title || "").slice(0, 40));
      for (const [p, d] of Object.entries(SEED.docs || {})) {
        this.q(`INSERT OR REPLACE INTO docs (path,data) VALUES (?,?)`, p, JSON.stringify(d));
      }
      this.lastTouch = 0;
      await this.touch();
      return json({ ok: true });
    }

    if (!this.keyHash) {
      await sleep(400); // slow down code guessing
      return json({ error: WRONG }, 404);
    }

    if (path === "/ws") return this.openSocket(request);

    try {
      // ---- players
      if (path === "/api/info") return json({ title: this.meta("title") || "" });
      if (path === "/api/join" && request.method === "POST") {
        const b = await request.json();
        const name = String(b.name || "").replace(/\s+/g, " ").trim().slice(0, 24) || "Player";
        const count = this.q(`SELECT COUNT(*) AS n FROM players`).one().n;
        if (count >= MAX_PLAYERS) return json({ error: "This game is full." }, 403);
        const pid = rid(4);
        const token = rid(16);
        this.q(`INSERT INTO players (pid,name,token,char,joined) VALUES (?,?,?,NULL,?)`, pid, name, token, Date.now());
        this.toDM({ type: "players", players: this.playerList() });
        await this.touch();
        return json({ token, pid, name, title: this.meta("title") || "" });
      }
      if (path === "/api/pcheck") {
        const p = this.playerByToken(url.searchParams.get("token"));
        return json({ ok: !!p }, p ? 200 : 401);
      }
      if (path === "/api/pchar" || path === "/api/psay" || path === "/api/proll") {
        const b = await request.json();
        const p = this.playerByToken(b.token);
        if (!p) return json({ error: "Please join again." }, 401);
        if (path === "/api/pchar") {
          const hid = b.char ? String(b.char) : null;
          let hero = null;
          if (hid) {
            hero = this.getDoc("heroes/" + hid);
            if (!hero) return json({ error: "No such character" }, 404);
          }
          this.q(`UPDATE players SET char=? WHERE pid=?`, hid, p.pid);
          this.toPlayer(p.pid, { type: "hero", hero, char: hid });
          this.toDM({ type: "players", players: this.playerList() });
          return json({ ok: true });
        }
        if (path === "/api/psay") {
          const text = String(b.text || "").trim().slice(0, 1000);
          if (text) this.addMessage(p.pid, "dm", text);
          return json({ ok: true });
        }
        const rec = b.roll || {};
        const hero = p.char ? this.getDoc("heroes/" + p.char) || {} : {};
        this.toDM({
          type: "proll",
          roll: {
            id: rid(5), t: Date.now(), who: String(hero.name || p.name).slice(0, 40), label: String(rec.label || "").slice(0, 40),
            expr: String(rec.expr || "").slice(0, 40), total: rec.total, detail: String(rec.detail || "").slice(0, 300),
            nat: rec.nat, secret: !!rec.secret,
          },
        });
        return json({ ok: true });
      }

      // ---- everything below is the DM only
      if (!(await this.dmOk(request.headers.get("X-DM-Key")))) {
        await sleep(400);
        return json({ error: "Not allowed" }, 403);
      }
      await this.touch();

      if (path === "/api/state") {
        return json({ docs: this.allDocs(), code: this.code, title: this.meta("title") || "", url: "", players: this.playerList(), chat: this.chatForDM() });
      }
      if (path === "/api/upload" && request.method === "POST") {
        const type = (request.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase();
        const n = Number(request.headers.get("Content-Length") || 0);
        if (!/^image\/(png|jpeg|gif|webp|svg\+xml)$/.test(type)) return json({ error: "Pictures only (PNG, JPG, GIF, WebP)." }, 415);
        if (!n || n > MAX_UPLOAD) return json({ error: "Picture too big (10 MB max)" }, 413);
        const id = rid(16) + "." + type.split("/")[1].replace("svg+xml", "svg").replace("jpeg", "jpg");
        await this.env.BUCKET.put(this.code + "/" + id, request.body, { httpMetadata: { contentType: type } });
        return json({ id, url: "/s/" + this.code + "/_blob/" + id });
      }

      const b = await request.json();
      if (path === "/api/set") {
        const p = String(b.path || "");
        if (!PATH_RE.test(p)) return json({ error: "bad path" }, 400);
        const data = b.data;
        if (data == null) this.q(`DELETE FROM docs WHERE path=?`, p);
        else {
          const s = JSON.stringify(data);
          if (s.length > MAX_DOC) return json({ error: "That is too big to save." }, 413);
          const n = this.q(`SELECT COUNT(*) AS n FROM docs`).one().n;
          if (n >= MAX_DOCS && !this.getDoc(p)) return json({ error: "Too many saved items." }, 413);
          this.q(`INSERT INTO docs (path,data) VALUES (?,?) ON CONFLICT(path) DO UPDATE SET data=excluded.data`, p, s);
        }
        this.toDM({ type: "doc", path: p, data: data ?? null });
        if (p.startsWith("heroes/")) {
          const hid = p.split("/")[1];
          for (const pl of this.q(`SELECT pid,char FROM players`)) {
            if (pl.char === hid) this.toPlayer(pl.pid, { type: "hero", hero: data ?? null });
            this.toPlayer(pl.pid, { type: "heroes", heroes: this.heroList() });
          }
        }
        return json({ ok: true });
      }
      if (path === "/api/title") {
        const title = String(b.title || "").replace(/\s+/g, " ").trim().slice(0, 40);
        this.setMeta("title", title);
        this.toDM({ type: "title", title });
        for (const p of this.q(`SELECT pid FROM players`)) this.toPlayer(p.pid, { type: "title", title });
        return json({ ok: true, title });
      }
      if (path === "/api/emit") {
        this.toDM({ type: "room", topic: String(b.topic || "").slice(0, 48), data: b.data, tab: b.tab });
        return json({ ok: true });
      }
      if (path === "/api/tell") {
        const to = b.to || "all";
        if (to !== "all" && !this.q(`SELECT 1 FROM players WHERE pid=?`, String(to)).toArray().length) return json({ error: "That player left" }, 404);
        let h = b.handout || null;
        if (h) h = { title: String(h.title || "").slice(0, 200), text: String(h.text || "").slice(0, 5000), img: String(h.img || "").slice(0, 200) || null };
        return json({ ok: true, msg: this.addMessage("dm", to, b.text, h) });
      }
      if (path === "/api/kick") {
        const pid = String(b.pid || "");
        this.q(`DELETE FROM players WHERE pid=?`, pid);
        for (const ws of this.ctx.getWebSockets("p:" + pid)) {
          this.send(ws, { type: "kicked" });
          try { ws.close(4001, "removed"); } catch {}
        }
        this.toDM({ type: "players", players: this.playerList() });
        return json({ ok: true });
      }
    } catch (e) {
      if (e instanceof SyntaxError) return json({ error: "bad request" }, 400);
      throw e;
    }
    return json({ error: "Not found" }, 404);
  }

  // ---------------------------------------------------------------- websockets
  async openSocket(request) {
    if (request.headers.get("Upgrade") !== "websocket") return new Response("Expected a WebSocket", { status: 426 });
    const origin = request.headers.get("Origin");
    if (origin && new URL(origin).host !== new URL(request.url).host) return new Response("Wrong origin", { status: 403 });
    const protos = this.protocols(request);
    const dmProto = protos.find((p) => p.startsWith("dm."));
    const pProto = protos.find((p) => p.startsWith("p."));

    if (dmProto) {
      if (!(await this.dmOk(dmProto.slice(3)))) return new Response("Not allowed", { status: 403 });
      const [client, server] = Object.values(new WebSocketPair());
      this.ctx.acceptWebSocket(server, ["dm"]);
      this.send(server, { type: "sync", docs: this.allDocs(), code: this.code, title: this.meta("title") || "", players: this.playerList(), chat: this.chatForDM() });
      await this.touch();
      return new Response(null, { status: 101, webSocket: client, headers: { "Sec-WebSocket-Protocol": dmProto } });
    }
    const p = pProto ? this.playerByToken(pProto.slice(2)) : null;
    if (!p) return new Response("Please join again", { status: 401 });
    const [client, server] = Object.values(new WebSocketPair());
    this.ctx.acceptWebSocket(server, ["p:" + p.pid]);
    const hero = p.char ? this.getDoc("heroes/" + p.char) : null;
    this.send(server, { type: "hello", title: this.meta("title") || "", name: p.name, char: p.char, hero, heroes: this.heroList(), messages: this.msgsFor(p.pid) });
    this.toDM({ type: "players", players: this.playerList() });
    return new Response(null, { status: 101, webSocket: client, headers: { "Sec-WebSocket-Protocol": pProto } });
  }

  webSocketMessage() {
    // Clients only send "ping" (answered automatically without waking this object).
  }
  webSocketClose(ws) {
    try { ws.close(); } catch {}
    this.toDM({ type: "players", players: this.playerList(ws) });
  }
  webSocketError(ws) {
    this.toDM({ type: "players", players: this.playerList(ws) });
  }

  // ---------------------------------------------------------------- cleanup
  async alarm() {
    // Idle for 30 days: delete the game and its pictures.
    let cursor;
    do {
      const l = await this.env.BUCKET.list({ prefix: this.code + "/", cursor, limit: 500 });
      if (l.objects.length) await this.env.BUCKET.delete(l.objects.map((o) => o.key));
      cursor = l.truncated ? l.cursor : undefined;
    } while (cursor);
    for (const ws of this.ctx.getWebSockets()) { try { ws.close(1001, "session ended"); } catch {} }
    await this.ctx.storage.deleteAlarm();
    await this.ctx.storage.deleteAll();
    this.keyHash = null;
  }
}
