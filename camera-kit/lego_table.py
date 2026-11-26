#!/usr/bin/env python3
"""
Lego D&D Table — projected map + ArUco-tracked minifigs + auto triggers.

Commands
  python lego_table.py --make-tags 0-15 --size-mm 16   # printable tag sheet (PDF + PNG)
  python lego_table.py --simulate                       # test map + triggers with the mouse, no camera
  python lego_table.py --calibrate minis                # recommended: 4 tagged minis on the corner squares
  python lego_table.py --calibrate projected            # projector shows 4 tags, camera finds them
  python lego_table.py                                  # run the game

Hotkeys (projector window or the GM camera window)
  Esc/q quit   g grid+IDs   f fog on/off   r reset fog+triggers   p light pads on/off
  Simulate mode: 0-9 pick a tag, left-click to place it, right-click to remove it.
"""
import argparse, json, math, os, sys, threading, time

import numpy as np
import cv2
import pygame

HERE = os.path.dirname(os.path.abspath(__file__))
ARUCO = cv2.aruco


# ---------------------------------------------------------------- ArUco helpers
def make_detector(dict_name):
    d = ARUCO.getPredefinedDictionary(getattr(ARUCO, dict_name))
    if hasattr(ARUCO, "ArucoDetector"):                      # OpenCV >= 4.7
        p = ARUCO.DetectorParameters()
        p.cornerRefinementMethod = ARUCO.CORNER_REFINE_SUBPIX
        det = ARUCO.ArucoDetector(d, p)
        return d, lambda gray: det.detectMarkers(gray)
    p = ARUCO.DetectorParameters_create()                   # older OpenCV
    p.cornerRefinementMethod = ARUCO.CORNER_REFINE_SUBPIX
    return d, lambda gray: ARUCO.detectMarkers(gray, d, parameters=p)


def marker_image(d, tag_id, px):
    if hasattr(ARUCO, "generateImageMarker"):
        return ARUCO.generateImageMarker(d, tag_id, px)
    return ARUCO.drawMarker(d, tag_id, px)


def parse_ids(spec):
    ids = []
    for part in spec.split(","):
        if "-" in part:
            a, b = part.split("-")
            ids += range(int(a), int(b) + 1)
        elif part.strip():
            ids.append(int(part))
    return ids


def make_tag_sheet(cfg, id_spec, size_mm, out_base):
    """Letter-size sheet at 300 DPI with cut guides. Print the PDF at 100% / Actual Size."""
    from PIL import Image
    d, _ = make_detector(cfg["aruco_dict"])
    dpi = 300
    mm = dpi / 25.4
    tag_px = int(round(size_mm * mm))
    pad = int(round(4 * mm))              # white quiet zone around each tag (keep it when cutting!)
    label_h = int(round(4 * mm))
    cell_w, cell_h = tag_px + 2 * pad, tag_px + 2 * pad + label_h
    W, H = int(8.5 * dpi), int(11 * dpi)
    margin = int(0.5 * dpi)
    sheet = np.full((H, W), 255, np.uint8)
    cols = max(1, (W - 2 * margin) // cell_w)
    ids = parse_ids(id_spec)
    for i, tag in enumerate(ids):
        r, c = divmod(i, cols)
        x, y = margin + c * cell_w, margin + r * cell_h
        if y + cell_h > H - margin:
            print(f"Sheet full; tags from {tag} on were left off. Use a smaller --size-mm or fewer IDs.")
            break
        sheet[y + pad:y + pad + tag_px, x + pad:x + pad + tag_px] = marker_image(d, tag, tag_px)
        cv2.rectangle(sheet, (x, y), (x + cell_w - 1, y + cell_h - 1), 200, 1)   # light cut guide
        name = cfg.get("players", {}).get(str(tag), "")
        cv2.putText(sheet, f"#{tag} {name}"[:18], (x + pad, y + 2 * pad + tag_px + label_h // 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, 0, 2)
    img = Image.fromarray(sheet)
    img.save(out_base + ".png", dpi=(dpi, dpi))
    img.convert("RGB").save(out_base + ".pdf", resolution=dpi)
    print(f"Wrote {out_base}.pdf and .png — {len(ids)} tags at {size_mm} mm. Print at 100% scale.")


# ---------------------------------------------------------------- Camera
class Camera:
    """Reads frames on a background thread so the projector never waits on the camera."""
    def __init__(self, c):
        if sys.platform.startswith("win"):
            backend = cv2.CAP_DSHOW
        elif sys.platform == "darwin":
            backend = cv2.CAP_AVFOUNDATION
        else:
            backend = cv2.CAP_ANY
        self.cap = cv2.VideoCapture(c.get("index", 0), backend)
        if not self.cap.isOpened():
            sys.exit(f"Could not open camera {c.get('index', 0)}. Try another camera.index in config.json.")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, c.get("width", 1920))
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, c.get("height", 1080))
        if c.get("exposure") is not None:       # values are camera/driver specific; experiment
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, c.get("auto_exposure_off_value", 0.25))
            self.cap.set(cv2.CAP_PROP_EXPOSURE, c["exposure"])
        self.frame, self.seq = None, 0
        self.lock = threading.Lock()
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self.running:
            ok, f = self.cap.read()
            if ok:
                with self.lock:
                    self.frame, self.seq = f, self.seq + 1
            else:
                time.sleep(0.01)

    def latest(self):
        with self.lock:
            return self.frame, self.seq

    def close(self):
        self.running = False
        time.sleep(0.05)
        self.cap.release()


# ---------------------------------------------------------------- Board geometry
class Board:
    """Fits a square grid onto the projector screen, centred."""
    def __init__(self, cols, rows, W, H):
        self.cols, self.rows, self.W, self.H = cols, rows, W, H
        self.cell = min(W / cols, H / rows)
        self.ox = (W - self.cell * cols) / 2
        self.oy = (H - self.cell * rows) / 2
        self.rect = pygame.Rect(round(self.ox), round(self.oy), round(self.cell * cols), round(self.cell * rows))

    def to_px(self, gx, gy):
        return self.ox + gx * self.cell, self.oy + gy * self.cell

    def to_grid(self, x, y):
        return (x - self.ox) / self.cell, (y - self.oy) / self.cell

    def cells_rect(self, c0, r0, c1, r1):          # inclusive cell range -> pixel rect
        x0, y0 = self.to_px(min(c0, c1), min(r0, r1))
        x1, y1 = self.to_px(max(c0, c1) + 1, max(r0, r1) + 1)
        return pygame.Rect(math.floor(x0), math.floor(y0), math.ceil(x1 - x0) + 1, math.ceil(y1 - y0) + 1)

    def proj_to_grid_matrix(self):
        s = 1 / self.cell
        return np.array([[s, 0, -self.ox * s], [0, s, -self.oy * s], [0, 0, 1]], np.float64)


def cells_from(spec):
    """Accepts {"cells": [[x,y],...]} and/or {"area": [x0,y0,x1,y1]} / {"areas": [...]}, inclusive."""
    cells = {tuple(c) for c in spec.get("cells", [])}
    areas = spec.get("areas", []) + ([spec["area"]] if "area" in spec else [])
    for x0, y0, x1, y1 in areas:
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                cells.add((x, y))
    return cells


# ---------------------------------------------------------------- Tracking
class Tracker:
    """Smooths jitter, ignores brief occlusion (hands!), and only 'settles' a mini after it rests."""
    def __init__(self, dwell, grace, forget=30.0, alpha=0.5, hysteresis=0.15):
        self.dwell, self.grace, self.forget = dwell, grace, forget
        self.alpha, self.hyst = alpha, hysteresis
        self.tags = {}

    def update(self, detections, now):
        for tid, (gx, gy) in detections.items():
            t = self.tags.get(tid)
            if t is None:
                t = self.tags[tid] = dict(gx=gx, gy=gy, cell=None, since=now, settled=None, last=now)
            else:
                t["gx"] += self.alpha * (gx - t["gx"])
                t["gy"] += self.alpha * (gy - t["gy"])
            t["last"] = now
            c = t["cell"]
            h = self.hyst
            inside_old = c is not None and c[0] - h <= t["gx"] < c[0] + 1 + h and c[1] - h <= t["gy"] < c[1] + 1 + h
            if not inside_old:
                t["cell"] = (math.floor(t["gx"]), math.floor(t["gy"]))
                t["since"] = now
        for tid in list(self.tags):
            t = self.tags[tid]
            gone = now - t["last"]
            if gone > self.forget:
                del self.tags[tid]
            elif gone <= self.grace and t["cell"] is not None and now - t["since"] >= self.dwell:
                t["settled"] = t["cell"]
            # while occluded (within grace) we keep the last settled cell, so nothing re-fires

    def visible(self, now):
        return {i: t for i, t in self.tags.items() if now - t["last"] <= self.grace}


class Trigger:
    def __init__(self, d):
        self.name = d.get("name", "trigger")
        tags = d.get("tags", "any")
        self.tags = "any" if tags == "any" else {int(t) for t in (tags if isinstance(tags, list) else [tags])}
        self.cells = cells_from(d)
        self.once = d.get("once", True)
        self.cooldown = d.get("cooldown", 0)
        self.enabled = d.get("enabled", True)
        self.actions = d.get("actions", [])
        self.reset()

    def reset(self):
        self.fired, self.last, self.inside = False, -1e9, set()

    def check(self, tracker, now, fire):
        if not self.enabled:
            return
        present = set()
        for tid, t in tracker.tags.items():
            if t["settled"] in self.cells and (self.tags == "any" or tid in self.tags):
                present.add(tid)
                if tid not in self.inside and (not self.once or not self.fired) and now - self.last >= self.cooldown:
                    self.fired, self.last = True, now
                    fire(self, tid)
        self.inside = present          # fires only on entering, not every frame


# ---------------------------------------------------------------- The table
class Table:
    def __init__(self, cfg, args):
        self.cfg, self.args = cfg, args
        g = cfg["grid"]
        self.cols, self.rows = g["cols"], g["rows"]
        self.players = {int(k): v for k, v in cfg.get("players", {}).items()}

        pygame.init()
        try:
            pygame.mixer.init()
        except pygame.error:
            print("No audio device; sounds disabled.")
        sizes = pygame.display.get_desktop_sizes()
        disp = cfg["projector"].get("display", 1)
        if args.simulate or args.windowed:
            W, H = cfg["projector"].get("window_size", [1280, 800])
            self.screen = pygame.display.set_mode((W, H))
        else:
            if disp >= len(sizes):
                print(f"Projector display {disp} not found ({len(sizes)} screen(s)); using display 0.")
                disp = 0
            W, H = sizes[disp]
            self.screen = pygame.display.set_mode((W, H), pygame.FULLSCREEN | pygame.NOFRAME, display=disp)
        pygame.display.set_caption("Lego Table — projector")
        self.board = Board(self.cols, self.rows, W, H)
        self.font = pygame.font.SysFont(None, max(14, int(self.board.cell * 0.45)))
        self.big = pygame.font.SysFont(None, max(24, int(self.board.cell * 1.2)))

        self.base = self._load_map(cfg["map"].get("base"))
        self.layers = {}
        for name, L in cfg["map"].get("layers", {}).items():
            self.layers[name] = dict(img=self._load_img(L.get("image")), visible=L.get("visible", False),
                                     start=L.get("visible", False))
        fog = cfg.get("fog", {})
        self.fog_on = fog.get("enabled", True)
        self.rooms = {n: cells_from(r if isinstance(r, dict) else {"areas": r}) for n, r in fog.get("rooms", {}).items()}
        self.start_revealed = set(fog.get("start_revealed", []))
        self.revealed = set(self.start_revealed)

        self.triggers = [Trigger(t) for t in cfg.get("triggers", [])]
        self.tracker = Tracker(cfg.get("dwell_seconds", 1.0), cfg.get("occlusion_grace_seconds", 2.0))
        self.sounds = {}
        self.flash = None
        self.show_grid = False
        self.pads = cfg.get("light_pads", True)
        self.log = []
        self.sim_tag = 0
        self.sim_tags = {}

    # ---- assets
    def _path(self, p):
        return p if (p is None or os.path.isabs(p)) else os.path.join(HERE, p)

    def _load_img(self, p):
        p = self._path(p)
        if not p or not os.path.exists(p):
            if p:
                print(f"Missing image: {p}")
            return None
        return pygame.transform.smoothscale(pygame.image.load(p).convert_alpha(), self.board.rect.size)

    def _load_map(self, p):
        img = self._load_img(p)
        if img:
            return img
        # Placeholder so everything works before you have art
        s = pygame.Surface(self.board.rect.size)
        s.fill((196, 178, 140))
        rng = np.random.default_rng(1)
        for _ in range(400):
            x, y = rng.integers(0, s.get_width()), rng.integers(0, s.get_height())
            pygame.draw.circle(s, (180, 160, 120), (int(x), int(y)), int(rng.integers(2, 8)))
        c = self.board.cell
        for i in range(self.cols + 1):
            pygame.draw.line(s, (120, 100, 70), (round(i * c), 0), (round(i * c), s.get_height()))
        for j in range(self.rows + 1):
            pygame.draw.line(s, (120, 100, 70), (0, round(j * c)), (s.get_width(), round(j * c)))
        return s

    def sound(self, p):
        if not pygame.mixer.get_init():
            return
        p = self._path(p)
        if p not in self.sounds:
            self.sounds[p] = pygame.mixer.Sound(p) if os.path.exists(p) else None
            if self.sounds[p] is None:
                print(f"Missing sound: {p}")
        if self.sounds[p]:
            self.sounds[p].play()

    # ---- triggers / actions
    def note(self, msg):
        print(msg)
        self.log = (self.log + [msg])[-6:]

    def fire(self, trig, tid):
        who = self.players.get(tid, f"tag {tid}")
        self.note(f"[{time.strftime('%H:%M:%S')}] {who} -> {trig.name}")
        for a in trig.actions:
            if "reveal" in a: self.revealed.add(a["reveal"])
            if "hide" in a: self.revealed.discard(a["hide"])
            if "show_layer" in a and a["show_layer"] in self.layers: self.layers[a["show_layer"]]["visible"] = True
            if "hide_layer" in a and a["hide_layer"] in self.layers: self.layers[a["hide_layer"]]["visible"] = False
            if "toggle_layer" in a and a["toggle_layer"] in self.layers:
                L = self.layers[a["toggle_layer"]]; L["visible"] = not L["visible"]
            if "sound" in a: self.sound(a["sound"])
            if "flash" in a:
                self.flash = (pygame.Color(a["flash"]), time.time(), a.get("seconds", 0.6))
            if "message" in a: self.note("   " + a["message"].replace("{player}", who))
            if "enable" in a or "disable" in a:
                for t in self.triggers:
                    if t.name == a.get("enable"): t.enabled = True
                    if t.name == a.get("disable"): t.enabled = False

    def reset(self):
        self.revealed = set(self.start_revealed)
        for L in self.layers.values():
            L["visible"] = L["start"]
        for t in self.triggers:
            t.reset()
            t.enabled = next((d.get("enabled", True) for d in self.cfg.get("triggers", []) if d.get("name") == t.name), True)
        self.note("Reset fog, layers and triggers.")

    # ---- drawing
    def draw(self, now):
        b, scr = self.board, self.screen
        scr.fill((0, 0, 0))
        scr.blit(self.base, b.rect.topleft)
        for L in self.layers.values():
            if L["visible"] and L["img"]:
                scr.blit(L["img"], b.rect.topleft)
        vis = self.tracker.visible(now)
        if self.pads:   # bright pad under each mini keeps its tag readable on dark map areas
            for t in self.tracker.tags.values():
                x, y = b.to_px(t["gx"], t["gy"])
                pygame.draw.circle(scr, (255, 255, 255), (int(x), int(y)), int(b.cell * 0.55))
        if self.fog_on:
            for name, cells in self.rooms.items():
                if name not in self.revealed:
                    for c, r in cells:
                        scr.fill((0, 0, 0), b.cells_rect(c, r, c, r))
        if self.show_grid:
            for i in range(self.cols + 1):
                x = round(b.ox + i * b.cell); pygame.draw.line(scr, (0, 255, 255), (x, b.rect.top), (x, b.rect.bottom))
            for j in range(self.rows + 1):
                y = round(b.oy + j * b.cell); pygame.draw.line(scr, (0, 255, 255), (b.rect.left, y), (b.rect.right, y))
            for tid, t in self.tracker.tags.items():
                x, y = b.to_px(t["gx"], t["gy"])
                col = (0, 200, 0) if t["settled"] == t["cell"] else (255, 160, 0)
                if tid not in vis: col = (150, 150, 150)
                lab = self.font.render(f"{tid} {t['cell']}", True, col, (0, 0, 0))
                scr.blit(lab, (x + b.cell * 0.4, y - b.cell * 0.4))
        if self.flash:
            color, t0, dur = self.flash
            k = 1 - (now - t0) / dur
            if k <= 0:
                self.flash = None
            else:
                ov = pygame.Surface(scr.get_size(), pygame.SRCALPHA)
                ov.fill((color.r, color.g, color.b, int(150 * k)))
                scr.blit(ov, (0, 0))
        if self.args.simulate:
            hint = self.font.render(f"SIMULATE  tag {self.sim_tag} selected (0-9), click to place, right-click remove", True, (255, 255, 0), (0, 0, 0))
            scr.blit(hint, (8, 8))
            for i, m in enumerate(self.log[-4:]):
                scr.blit(self.font.render(m, True, (255, 255, 255), (0, 0, 0)), (8, 8 + (i + 1) * hint.get_height()))
        pygame.display.flip()

    # ---- input
    def handle_key(self, ch):
        if ch in ("q", "\x1b"): return False
        if ch == "g": self.show_grid = not self.show_grid
        if ch == "f": self.fog_on = not self.fog_on
        if ch == "r": self.reset()
        if ch == "p": self.pads = not self.pads
        if self.args.simulate and ch.isdigit(): self.sim_tag = int(ch)
        return True

    def pump(self):
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                return False
            if e.type == pygame.KEYDOWN:
                if e.key == pygame.K_ESCAPE or not self.handle_key(e.unicode or ""):
                    return False
            if self.args.simulate and e.type == pygame.MOUSEBUTTONDOWN:
                gx, gy = self.board.to_grid(*e.pos)
                if e.button == 1 and 0 <= gx < self.cols and 0 <= gy < self.rows:
                    self.sim_tags[self.sim_tag] = (math.floor(gx) + 0.5, math.floor(gy) + 0.5)
                elif e.button == 3:
                    near = min(self.sim_tags, key=lambda k: math.dist(self.sim_tags[k], (gx, gy)), default=None)
                    if near is not None and math.dist(self.sim_tags[near], (gx, gy)) < 1.0:
                        del self.sim_tags[near]
        return True


# ---------------------------------------------------------------- Calibration
def load_calibration(cfg):
    p = os.path.join(HERE, cfg.get("calibration_file", "calibration.json"))
    if not os.path.exists(p):
        sys.exit("No calibration found. Run:  python lego_table.py --calibrate minis   (or projected)")
    c = json.load(open(p))
    if (c["cols"], c["rows"]) != (cfg["grid"]["cols"], cfg["grid"]["rows"]):
        sys.exit("Grid size changed since calibration. Please recalibrate.")
    return np.array(c["H"], np.float64)


def save_calibration(cfg, H, mode):
    p = os.path.join(HERE, cfg.get("calibration_file", "calibration.json"))
    json.dump(dict(H=H.tolist(), cols=cfg["grid"]["cols"], rows=cfg["grid"]["rows"], mode=mode,
                   when=time.strftime("%Y-%m-%d %H:%M")), open(p, "w"), indent=2)
    print(f"Saved calibration to {p}")


def calibrate(cfg, args, table, cam, detect, mode):
    b, scr = table.board, table.screen
    d, _ = make_detector(cfg["aruco_dict"])
    W, H = scr.get_size()
    if mode == "projected":
        ids = cfg.get("projected_calibration_ids", [46, 47, 48, 49])
        s = int(min(W, H) * 0.14)
        m = int(s * 0.35)
        spots = [(b.rect.left + m, b.rect.top + m), (b.rect.right - m - s, b.rect.top + m),
                 (b.rect.right - m - s, b.rect.bottom - m - s), (b.rect.left + m, b.rect.bottom - m - s)]
        targets = {}
        scr.fill((255, 255, 255))
        for tid, (x, y) in zip(ids, spots):
            img = cv2.cvtColor(marker_image(d, tid, s), cv2.COLOR_GRAY2RGB)
            scr.blit(pygame.surfarray.make_surface(img.swapaxes(0, 1)), (x, y))
            targets[tid] = np.array([[x, y], [x + s, y], [x + s, y + s], [x, y + s]], np.float64)
        msg = "Clear the table. Looking for the 4 projected tags..."
    else:
        ids = cfg.get("mini_calibration_ids", [0, 1, 2, 3])
        corners = [(0, 0), (b.cols - 1, 0), (b.cols - 1, b.rows - 1), (0, b.rows - 1)]
        targets = {tid: np.array([[c + 0.5, r + 0.5]], np.float64) for tid, (c, r) in zip(ids, corners)}
        scr.fill((60, 60, 60))
        scr.blit(table.base, b.rect.topleft)
        for tid, (c, r) in zip(ids, corners):
            rc = b.cells_rect(c, r, c, r)
            pygame.draw.rect(scr, (255, 0, 0), rc, 4)
            lab = table.big.render(str(tid), True, (255, 0, 0))
            scr.blit(lab, lab.get_rect(center=(rc.centerx + (b.cell * 1.3 if c == 0 else -b.cell * 1.3),
                                               rc.centery + (b.cell * 1.3 if r == 0 else -b.cell * 1.3))))
        msg = f"Stand minis with tags {ids} on the red corner squares..."
    pygame.display.flip()
    print(msg)

    hist, last_seq = [], -1
    while True:
        if not table.pump():
            return None
        frame, seq = cam.latest()
        if frame is None or seq == last_seq:
            cv2.waitKey(10); continue
        last_seq = seq
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        corners, found, _ = detect(gray)
        got = {}
        if found is not None:
            for cs, tid in zip(corners, found.flatten()):
                if int(tid) in targets:
                    got[int(tid)] = cs.reshape(4, 2) if mode == "projected" else cs.reshape(4, 2).mean(0, keepdims=True)
        view = frame.copy()
        if found is not None:
            ARUCO.drawDetectedMarkers(view, corners, found)
        cv2.putText(view, f"{msg}  found {sorted(got)}", (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
        show_gm(view)
        if cv2.waitKey(1) & 0xFF in (27, ord("q")):
            return None
        hist = hist + [got] if len(got) == len(targets) else []
        if len(hist) >= 15:
            break
    src = np.vstack([np.mean([h[t] for h in hist], axis=0) for t in targets])
    dst = np.vstack([targets[t] for t in targets])
    Hm, _ = cv2.findHomography(src, dst)
    if mode == "projected":
        Hm = b.proj_to_grid_matrix() @ Hm
    save_calibration(cfg, Hm, mode)
    return Hm


# ---------------------------------------------------------------- GM view
def show_gm(img, width=960):
    h, w = img.shape[:2]
    if w > width:
        img = cv2.resize(img, (width, int(h * width / w)))
    cv2.imshow("Lego Table — GM camera view", img)


# ---------------------------------------------------------------- Main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=os.path.join(HERE, "config.json"))
    ap.add_argument("--calibrate", choices=["minis", "projected"])
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--windowed", action="store_true", help="projector output in a window (testing)")
    ap.add_argument("--make-tags", metavar="IDS", help="e.g. 0-15 or 0,1,2,7")
    ap.add_argument("--size-mm", type=float, default=16)
    args = ap.parse_args()
    cfg = json.load(open(args.config))

    if args.make_tags:
        make_tag_sheet(cfg, args.make_tags, args.size_mm, os.path.join(HERE, "tags"))
        return

    table = Table(cfg, args)
    _, detect = make_detector(cfg["aruco_dict"])
    cam = None
    if not args.simulate:
        cam = Camera(cfg["camera"])
        if args.calibrate:
            ok = calibrate(cfg, args, table, cam, detect, args.calibrate) is not None
            cam.close(); pygame.quit(); cv2.destroyAllWindows()
            print("Calibration done." if ok else "Calibration cancelled.")
            return
        Hm = load_calibration(cfg)

    clock, last_seq, running = pygame.time.Clock(), -1, True
    while running:
        now = time.time()
        running = table.pump()
        if args.simulate:
            table.tracker.update(dict(table.sim_tags), now)
        else:
            frame, seq = cam.latest()
            if frame is not None and seq != last_seq:
                last_seq = seq
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                corners, found, _ = detect(gray)
                dets = {}
                if found is not None:
                    centers = np.array([c.reshape(4, 2).mean(0) for c in corners], np.float64).reshape(-1, 1, 2)
                    grid = cv2.perspectiveTransform(centers, Hm).reshape(-1, 2)
                    dets = {int(t): (float(g[0]), float(g[1])) for t, g in zip(found.flatten(), grid)
                            if -1 <= g[0] <= table.cols + 1 and -1 <= g[1] <= table.rows + 1}
                table.tracker.update(dets, now)
                view = frame.copy()
                if found is not None:
                    ARUCO.drawDetectedMarkers(view, corners, found)
                y = 40
                for tid, t in sorted(table.tracker.tags.items()):
                    state = "settled" if t["settled"] == t["cell"] else "moving"
                    cv2.putText(view, f"{table.players.get(tid, tid)}: {t['cell']} {state}", (20, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2); y += 32
                for m in table.log[-5:]:
                    cv2.putText(view, m, (20, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 255), 2); y += 30
                show_gm(view)
            k = cv2.waitKey(1) & 0xFF
            if k != 255 and not table.handle_key(chr(k)):
                running = False
        for t in table.triggers:
            t.check(table.tracker, now, table.fire)
        table.draw(now)
        clock.tick(30)

    if cam:
        cam.close()
    pygame.quit()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
