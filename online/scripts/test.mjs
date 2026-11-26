// End-to-end check against a running `wrangler dev` (default http://localhost:8799).
// Node 22+ has fetch and WebSocket built in.
const B = process.env.BASE || "http://localhost:8799";
const WS = B.replace(/^http/, "ws");
let fails = 0;
const ok = (c, m) => { console.log((c ? "PASS " : "FAIL ") + m); if (!c) fails++; };
const J = async (u, o = {}) => { const r = await fetch(B + u, o); return { r, j: await r.json().catch(() => ({})) }; };
const post = (u, b, h = {}) => J(u, { method: "POST", headers: { "Content-Type": "application/json", ...h }, body: JSON.stringify(b) });
const sock = (path, proto) => new Promise((res, rej) => {
  const ws = new WebSocket(WS + path, [proto]); const got = [];
  ws.onmessage = (e) => { if (e.data !== "pong") got.push(JSON.parse(e.data)); };
  ws.onopen = () => res({ ws, got }); ws.onerror = () => rej(new Error("ws failed"));
});
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const { j: s } = await post("/api/create", {});
ok(/^[A-Z]+-\d\d$/.test(s.code) && s.key, "create gives code " + s.code);
const P = "/s/" + s.code, K = { "X-DM-Key": s.key };

ok((await J("/s/NOPE-00/api/state")).r.status === 404, "unknown code refused");
ok((await J(P + "/api/state")).r.status === 403, "state without DM key refused");
ok((await J(P + "/api/state", { headers: { "X-DM-Key": "wrong" } })).r.status === 403, "state with wrong key refused");
const st = (await J(P + "/api/state", { headers: K })).j;
ok(Object.keys(st.docs).filter((p) => p.startsWith("monsters/")).length >= 20, "seed monsters loaded");

const dm = await sock(P + "/ws", "dm." + s.key);
ok(true, "DM socket open");
await post(P + "/api/set", { path: "heroes/h1", data: { name: "Thorn", hp: 20, hpMax: 20, cls: "Fighter" } }, K);
const { r: jr, j: pj } = await post(P + "/api/join", { name: "Sam" });
ok(jr.status === 200 && pj.token, "player joins");
const pl = await sock(P + "/ws", "p." + pj.token);
await wait(300);
const hello = pl.got.find((m) => m.type === "hello");
ok(hello && hello.heroes.some((h) => h.id === "h1"), "player hello lists hero");
await post(P + "/api/pchar", { token: pj.token, char: "h1" });
await post(P + "/api/set", { path: "heroes/h1", data: { name: "Thorn", hp: 11, hpMax: 20 } }, K);
await wait(300);
ok(pl.got.some((m) => m.type === "hero" && m.hero && m.hero.hp === 11), "live HP reaches player");
await post(P + "/api/psay", { token: pj.token, text: "hi dm" });
await post(P + "/api/proll", { token: pj.token, roll: { expr: "1d20", total: 17, nat: 17 } });
await wait(300);
ok(dm.got.some((m) => m.type === "chat" && m.msg.text === "hi dm"), "chat reaches DM");
ok(dm.got.some((m) => m.type === "proll" && m.roll.total === 17 && m.roll.who === "Thorn"), "roll reaches DM");
await post(P + "/api/tell", { to: pj.pid, text: "secret" }, K);
const { j: p2 } = await post(P + "/api/join", { name: "Ann" });
const pl2 = await sock(P + "/ws", "p." + p2.token);
await post(P + "/api/tell", { to: pj.pid, text: "only sam" }, K);
await wait(300);
ok(pl.got.some((m) => m.type === "msg" && m.msg.text === "only sam"), "private message reaches its player");
ok(!pl2.got.some((m) => m.type === "msg" && m.msg.text === "only sam"), "private message hidden from others");
await post(P + "/api/emit", { topic: "ping", data: { x: 1 }, tab: "t" }, K);
await wait(200);
ok(dm.got.some((m) => m.type === "room" && m.topic === "ping"), "room emit relayed");
ok((await post(P + "/api/set", { path: "x/y", data: 1 })).r.status === 403, "player cannot write docs");
const png = Uint8Array.from(atob("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="), (c) => c.charCodeAt(0));
const up = await J(P + "/api/upload", { method: "POST", headers: { ...K, "Content-Type": "image/png" }, body: png });
ok(up.r.status === 200 && up.j.id, "upload picture");
const img = await fetch(B + P + "/_blob/" + up.j.id);
ok(img.status === 200 && img.headers.get("content-type") === "image/png", "picture served from R2");
ok((await J(P + "/api/upload", { method: "POST", headers: { ...K, "Content-Type": "text/html" }, body: "<script>" })).r.status === 415, "non-image upload refused");
await post(P + "/api/kick", { pid: p2.pid }, K);
await wait(300);
ok((await J(P + "/api/pcheck?token=" + p2.token)).r.status === 401, "kicked player's token dead");
const dm2 = await sock(P + "/ws", "dm." + s.key); await wait(300);
ok(dm2.got[0]?.type === "sync" && dm2.got[0].players.length === 1, "reconnecting DM gets a full sync");
for (const x of [dm.ws, pl.ws, pl2.ws, dm2.ws]) try { x.close(); } catch {}
console.log(fails ? fails + " FAILED" : "ALL PASSED");
process.exit(fails ? 1 : 0);
