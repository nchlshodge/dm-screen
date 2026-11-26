// The DM page and the player page are shared with the rest of the repo.
// Copy them into public/ so one source of truth feeds every edition.
import { copyFileSync } from "node:fs";
const root = new URL("../../", import.meta.url);
const out = new URL("../public/", import.meta.url);
copyFileSync(new URL("web/index.html", root), new URL("dm.html", out));
copyFileSync(new URL("local-edition/player.html", root), new URL("play.html", out));
console.log("Synced dm.html and play.html into online/public/");
