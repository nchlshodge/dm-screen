// DM Screen online: routes every request to the right session (one Durable Object per code).
//   /                    landing page (start a session, or join with a code)
//   /api/create          POST: make a new session, returns { code, key }
//   /s/<CODE>/           player page
//   /s/<CODE>/dm         DM page (the DM key travels in ?k= once, then lives in the browser)
//   /s/<CODE>/api/*      the game API   (handled by the Session object)
//   /s/<CODE>/ws         live connection (handled by the Session object)
//   /s/<CODE>/_blob/<id> pictures from R2
import { WORDS } from "./words.js";
import { sha256 } from "./util.js";
export { Session } from "./session.js";

const CODE_RE = /^[A-Z]{2,8}-\d{2}$/;

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), { status, headers: { "Content-Type": "application/json", "Cache-Control": "no-store" } });

function newCode() {
  const r = new Uint32Array(2);
  crypto.getRandomValues(r);
  return WORDS[r[0] % WORDS.length] + "-" + String(10 + (r[1] % 90));
}

function newKey() {
  const b = new Uint8Array(24);
  crypto.getRandomValues(b);
  return btoa(String.fromCharCode(...b)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

async function asset(env, request, file, type = "text/html; charset=utf-8") {
  const r = await env.ASSETS.fetch(new Request(new URL(file, request.url), { method: "GET" }));
  if (!r.ok) return new Response("Not found", { status: 404 });
  const h = new Headers(r.headers);
  h.set("Content-Type", type);
  h.set("Cache-Control", "no-cache");
  h.set("Referrer-Policy", "no-referrer"); // the DM link carries a secret in ?k=
  return new Response(r.body, { status: 200, headers: h });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (path === "/" || path === "/index.html") return asset(env, request, "/index.html");

    if (path === "/api/create" && request.method === "POST") {
      const body = await request.json().catch(() => ({}));
      const title = String(body.title || "").replace(/\s+/g, " ").trim().slice(0, 40);
      for (let i = 0; i < 8; i++) {
        const code = newCode();
        const key = newKey();
        const stub = env.SESSION.get(env.SESSION.idFromName(code));
        const r = await stub.fetch("https://session/init", {
          method: "POST",
          body: JSON.stringify({ code, title, keyHash: await sha256(key) }),
        });
        if (r.status === 200) return json({ code, key, title });
      }
      return json({ error: "Could not make a code. Try again." }, 503);
    }

    const m = path.match(/^\/s\/([^/]+)(\/.*)?$/);
    if (m) {
      const code = decodeURIComponent(m[1]).toUpperCase();
      const rest = m[2] || "/";
      if (!CODE_RE.test(code)) return json({ error: "That code doesn't match. Check the DM's screen." }, 404);
      if (m[1] !== code) return Response.redirect(url.origin + "/s/" + code + rest + url.search, 302);

      if (rest === "/") return asset(env, request, "/play.html");
      if (rest === "/dm" || rest === "/dm/") return asset(env, request, "/dm.html");

      if (rest.startsWith("/_blob/")) {
        const id = decodeURIComponent(rest.slice(7)).replace(/[^A-Za-z0-9._-]/g, "");
        const obj = id ? await env.BUCKET.get(code + "/" + id) : null;
        if (!obj) return new Response("Not found", { status: 404 });
        return new Response(obj.body, {
          headers: {
            "Content-Type": obj.httpMetadata?.contentType || "application/octet-stream",
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'", // an uploaded SVG can't run scripts
          },
        });
      }

      if (rest.startsWith("/api/") || rest === "/ws") {
        const stub = env.SESSION.get(env.SESSION.idFromName(code));
        const fwd = new URL(request.url);
        fwd.pathname = rest;
        const req = new Request(fwd, request);
        req.headers.set("X-Session-Code", code);
        return stub.fetch(req);
      }
    }

    return env.ASSETS.fetch(request);
  },
};
