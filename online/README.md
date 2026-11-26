# DM Screen: online sessions

Play from anywhere. The DM clicks **Start a new game** and gets a code like `WOLF-42`; players open the site, type the code and their first name. No accounts.

Runs on Cloudflare's free plan: one Worker, one Durable Object per game (SQLite + WebSockets), pictures in R2.

## Deploy (one time, about 10 minutes)

1. Make a free account at https://dash.cloudflare.com and, in R2, turn R2 on (it asks for a payment method but the free tier covers this).
2. In a terminal, from this `online/` folder:

```bash
npm install
npx wrangler login
npx wrangler r2 bucket create dm-screen-pictures
npm run deploy
```

Wrangler prints your address, like `https://dm-screen.<you>.workers.dev`. Open it.

## Try it on your own computer

```bash
npm install
npm run dev          # http://localhost:8799 is printed; use that
npm test             # in a second terminal: BASE=http://localhost:<port> npm test
```

## How it fits together

| URL | What |
| --- | --- |
| `/` | Landing page: join with a code, or start a game |
| `/s/<CODE>/` | Player page (`local-edition/player.html`) |
| `/s/<CODE>/dm` | DM page (`web/index.html`); `#table` opens the table on the DM's own computer |
| `/s/<CODE>/table?t=<KEY>` | View-only table screen for a projector or TV computer, with its own key |
| `/s/<CODE>/api/*`, `/s/<CODE>/ws` | Game API and live connection (the `Session` object) |
| `/s/<CODE>/_blob/<id>` | Pictures from R2 |

- `npm run sync` (runs before `dev` and `deploy`) copies `web/index.html` and `local-edition/player.html` into `public/`, so there is one source for every edition.
- The DM link carries a secret key (`?k=...`). The page saves it in the browser and removes it from the address bar. Only a hash of the key is stored on the server. Use **Copy DM link** on the Players tab to open the game on another device.
- The table link (Map tab, Table screen setup) has its own view-only key. It receives only the map, combat, public dice rolls, the party and the handout on screen: never the monster library, secret rolls, chat or players. "Make a new link" replaces it.
- Characters can be saved to a file and loaded into any edition (Party tab, **Move characters**). Imported files are cleaned and always add new characters; they never overwrite existing ones.
- Players get a token when they join and can only see their own sheet and messages.
- A game nobody touches for 30 days is deleted with its pictures. Pictures: images only, 10 MB each.

## Limits to know about

- Players can still be guessed into a game if someone guesses a live code (about 18,000 possible). The DM can remove anyone from the Players tab. A player cap of 12 applies.
- Player actions (message, roll, pick character) use ordinary requests; live updates use the WebSocket.
