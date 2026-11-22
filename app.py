# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "flask>=3", "pillow>=10", "pillow-heif>=0.16",
#   "opencv-python-headless>=4.8", "numpy>=1.26",
#   "pyobjc-framework-Vision>=10; sys_platform == 'darwin'",
# ]
# ///
"""PlayBillDisplay: a Playbill library on the Mac, shown on a jailbroken Kindle.

Run:  uv run app.py          then open http://localhost:8780

Drop photos in, browse and tag them (show, theatre, date, cast read off the cast page),
tune the e-ink conversion, and pick what the Kindle shows. On the Kindle, cron runs
kindle/playbills/update_playbill.sh every minute; it pulls images from /kindle/list.txt
and /kindle/img/<name>, applies /kindle/command.txt, and draws with eips.
"""
import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from PIL import UnidentifiedImageError

import imaging
import ocr

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "library")
RAW = os.path.join(LIB, "raw")        # original cover photos
PAGES = os.path.join(LIB, "pages")    # extra photos (cast pages etc.)
OUT = os.path.join(LIB, "kindle")     # converted e-ink PNGs
THUMBS = os.path.join(LIB, "thumbs")  # browser-friendly JPEGs of originals
INDEX = os.path.join(LIB, "index.json")
DISPLAY = os.path.join(LIB, "display.json")  # what the user asked the Kindle to show
KINDLE_SCRIPTS = os.path.join(HERE, "kindle", "playbills")  # copied to /mnt/us/playbills

PORT = int(os.environ.get("PORT", "8780"))
# Paperwhite 11th gen. Override for other models, e.g. W=1072 H=1448 (PW 3/4), W=600 H=800 (basic).
W = int(os.environ.get("W", "1236"))
H = int(os.environ.get("H", "1648"))
MARGIN = int(os.environ.get("MARGIN", "24"))
KINDLE_MOUNT = os.environ.get("KINDLE", "/Volumes/Kindle")
SAFE_NAME = re.compile(r"[\w.-]+")

_lock = threading.Lock()
app = Flask(__name__, static_folder=os.path.join(HERE, "static"), static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024


# ---------- library ----------

def load_index() -> list[dict]:
    try:
        with open(INDEX) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_json(path: str, data, indent: int | None = None) -> None:
    """Write via a temp file so a crash never leaves half a file behind."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
    os.replace(tmp, path)


def save_index(items: list[dict]) -> None:
    _write_json(INDEX, items, indent=2)


def load_display() -> dict:
    """{"seq": n, "show": item_id|None, "pin": bool}. seq bumps on every request so the
    Kindle can tell a new command from one it already applied."""
    try:
        with open(DISPLAY) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seq": 0, "show": None, "pin": False}


def save_display(d: dict) -> None:
    _write_json(DISPLAY, d)


def request_display(show: str | None, pin: bool = False) -> dict:
    """Ask the Kindle to show item `show` (None = resume normal rotation). Call with _lock held."""
    d = load_display()
    d.update(seq=d["seq"] + 1, show=show, pin=pin)
    save_display(d)
    return d


# Last report from the Kindle: which file it is showing and when it checked in.
_kindle_seen = {"showing": None, "at": 0.0}


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", name).strip("-").lower()
    return s[:40] or "playbill"


def normalize(item: dict) -> dict:
    """Fill fields added after the first version so old items keep working."""
    item.setdefault("info", {"show": item.get("title", ""), "theatre": "", "date": "", "notes": ""})
    item.setdefault("cast", "")
    item.setdefault("pages", [])
    item["enhance"] = imaging.settings_for(item)
    item.pop("rotation", None)
    item["title"] = item["info"].get("show") or item.get("title") or item["id"]
    return item


def build(item: dict) -> None:
    """(Re)render item's PNG. Output name embeds a content hash so the Kindle sees changes."""
    data = imaging.render_png(os.path.join(RAW, item["raw"]), item["enhance"], (W, H), MARGIN)
    name = f"{item['id']}-{hashlib.sha1(data).hexdigest()[:8]}.png"
    with open(os.path.join(OUT, name), "wb") as f:
        f.write(data)
    old = item.get("out")
    if old and old != name:
        try:
            os.remove(os.path.join(OUT, old))
        except FileNotFoundError:
            pass
    item["out"] = name


def find(items: list[dict], item_id: str) -> dict:
    for it in items:
        if it["id"] == item_id:
            return normalize(it)
    abort(404)


def thumb(src_dir: str, name: str) -> str:
    """Path to a cached JPEG of an original (HEIC/PNG/huge JPEG) for the browser."""
    if not SAFE_NAME.fullmatch(name):
        abort(404)
    src = os.path.join(src_dir, name)
    if not os.path.isfile(src):
        abort(404)
    dst = os.path.join(THUMBS, name + ".jpg")
    if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
        with open(dst, "wb") as f:
            f.write(imaging.thumbnail_jpeg(src))
    return dst


def remove_files(*paths: str) -> None:
    for p in paths:
        if p and os.path.isfile(p):
            os.remove(p)


# ---------- network / usb helpers ----------

def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # no packet sent; just picks the outbound interface
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def server_url() -> str:
    return f"http://{lan_ip()}:{PORT}"


def kindle_mounted() -> bool:
    return os.path.isdir(KINDLE_MOUNT) and os.path.isdir(os.path.join(KINDLE_MOUNT, "documents"))


# ---------- routes: UI API ----------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/state")
def state():
    return jsonify(
        items=[normalize(it) for it in load_index()],
        server_url=server_url(),
        kindle_mounted=kindle_mounted(),
        kindle_mount=KINDLE_MOUNT,
        resolution=[W, H],
        display=load_display(),
        kindle=_kindle_seen,
        presets=imaging.PRESETS,
        ocr=ocr.AVAILABLE,
    )


@app.post("/api/upload")
def upload():
    files = request.files.getlist("files")
    added, errors = [], []
    for n, fs in enumerate(files):
        fname = fs.filename or "playbill"
        stem, ext = os.path.splitext(os.path.basename(fname))
        item_id = time.strftime("%Y%m%d%H%M%S") + f"{n % 100:02d}-{slugify(stem)}"
        raw_name = item_id + (ext.lower() or ".jpg")
        raw_path = os.path.join(RAW, raw_name)
        fs.save(raw_path)
        item = normalize({"id": item_id, "title": stem, "raw": raw_name})
        try:
            build(item)
        except (UnidentifiedImageError, OSError, ValueError) as e:
            remove_files(raw_path)
            errors.append(f"{fname}: {e}")
            continue
        if ocr.AVAILABLE:  # guess show/theatre from the cover
            try:
                guess = ocr.cover_info(raw_path, item["enhance"]["crop"], item["enhance"]["rotation"])
                item["info"].update({k: v for k, v in guess.items() if v})
                normalize(item)
            except Exception as e:  # OCR is a nicety; never fail the upload over it
                app.logger.warning("cover OCR failed for %s: %s", fname, e)
        with _lock:
            items = load_index()
            items.append(item)
            save_index(items)
        added.append(item)
    return jsonify(added=added, errors=errors)


@app.patch("/api/item/<item_id>")
def update_item(item_id):
    """Body: {"info": {show, theatre, date, notes}, "cast": "..."} (either part optional)."""
    body = request.get_json(silent=True) or {}
    with _lock:
        items = load_index()
        it = find(items, item_id)
        if isinstance(body.get("info"), dict):
            for k in ("show", "theatre", "date", "notes"):
                if k in body["info"]:
                    it["info"][k] = str(body["info"][k])[:5000]
        if "cast" in body:
            it["cast"] = str(body["cast"])[:50000]
        normalize(it)
        save_index(items)
    return jsonify(it)


@app.post("/api/item/<item_id>/enhance")
def enhance(item_id):
    """Save conversion settings and re-render the Kindle image."""
    s = imaging.clean_settings(request.get_json(silent=True) or {})
    with _lock:
        items = load_index()
        it = find(items, item_id)
        it["enhance"] = s
        build(it)
        save_index(items)
    return jsonify(it)


@app.post("/api/item/<item_id>/preview")
def preview_settings(item_id):
    """Render with unsaved settings at half size, for live preview in the editor."""
    s = imaging.clean_settings(request.get_json(silent=True) or {})
    it = find(load_index(), item_id)
    data = imaging.render_png(os.path.join(RAW, it["raw"]), s, (W // 2, H // 2), MARGIN // 2)
    return Response(data, mimetype="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/item/<item_id>/corners")
def corners(item_id):
    """Where the Playbill is in the photo: the saved manual corners, or auto-detected
    ones, or the whole photo. 0..1 coords of the upright photo, clockwise from top-left."""
    it = find(load_index(), item_id)
    crop = it["enhance"]["crop"]
    if isinstance(crop, list):
        return jsonify(corners=crop, source="manual")
    found = imaging.detect_corners(imaging.open_photo(os.path.join(RAW, it["raw"])))
    if found:
        return jsonify(corners=found, source="auto")
    return jsonify(corners=[[0, 0], [1, 0], [1, 1], [0, 1]], source="none")


@app.post("/api/item/<item_id>/rotate")
def rotate(item_id):
    with _lock:
        items = load_index()
        it = find(items, item_id)
        it["enhance"]["rotation"] = (it["enhance"].get("rotation", 0) + 90) % 360
        build(it)
        save_index(items)
    return jsonify(it)


@app.post("/api/rebuild")
def rebuild():
    """Re-render every item (after changing conversion code)."""
    with _lock:
        items = load_index()
        for it in items:
            build(normalize(it))
        save_index(items)
    return jsonify(ok=True, count=len(items))


@app.post("/api/item/<item_id>/read-cover")
def read_cover(item_id):
    """Re-run cover text recognition; returns the guess without saving."""
    it = find(load_index(), item_id)
    try:
        e = it["enhance"]
        return jsonify(ocr.cover_info(os.path.join(RAW, it["raw"]), e["crop"], e["rotation"]))
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.post("/api/item/<item_id>/pages")
def add_pages(item_id):
    """Attach photos of inside pages (cast list). Each is read; the text comes back as
    lines for the user to review. Nothing is written to the cast field here."""
    find(load_index(), item_id)
    results, errors = [], []
    for n, fs in enumerate(request.files.getlist("files")):
        ext = os.path.splitext(fs.filename or "")[1].lower() or ".jpg"
        name = f"{item_id}-p{time.strftime('%H%M%S')}{n:02d}{ext}"
        path = os.path.join(PAGES, name)
        fs.save(path)
        try:
            imaging.open_photo(path)
        except (UnidentifiedImageError, OSError) as e:
            remove_files(path)
            errors.append(f"{fs.filename}: {e}")
            continue
        lines, err = [], None
        try:
            lines = ocr.cast_lines(path)
        except Exception as e:
            err = str(e)
        results.append({"page": name, "lines": lines, "error": err})
    with _lock:
        items = load_index()
        it = find(items, item_id)
        it["pages"] += [r["page"] for r in results]
        save_index(items)
    return jsonify(results=results, errors=errors, item=it)


@app.post("/api/item/<item_id>/pages/<name>/read")
def reread_page(item_id, name):
    it = find(load_index(), item_id)
    if name not in it["pages"]:
        abort(404)
    try:
        return jsonify(lines=ocr.cast_lines(os.path.join(PAGES, name)))
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.delete("/api/item/<item_id>/pages/<name>")
def delete_page(item_id, name):
    with _lock:
        items = load_index()
        it = find(items, item_id)
        if name not in it["pages"]:
            abort(404)
        it["pages"].remove(name)
        remove_files(os.path.join(PAGES, name), os.path.join(THUMBS, name + ".jpg"))
        save_index(items)
    return jsonify(it)


@app.get("/photo/<name>")
def photo(name):
    """Original cover photo as a browser-friendly JPEG."""
    return send_from_directory(THUMBS, os.path.basename(thumb(RAW, name)))


@app.get("/cover/<item_id>")
def cover(item_id):
    """Color cover, cropped like the Kindle image. Cached per crop/rotation."""
    it = find(load_index(), item_id)
    e = it["enhance"]
    key = hashlib.sha1(json.dumps([it["raw"], e["crop"], e["rotation"]]).encode()).hexdigest()[:10]
    name = f"{item_id}-cover-{key}.jpg"
    path = os.path.join(THUMBS, name)
    if not os.path.exists(path):
        with open(path, "wb") as f:
            f.write(imaging.cover_jpeg(os.path.join(RAW, it["raw"]), e))
    return send_from_directory(THUMBS, name, max_age=31536000)


@app.get("/page/<name>")
def page_photo(name):
    return send_from_directory(THUMBS, os.path.basename(thumb(PAGES, name)))


@app.post("/api/item/<item_id>/show")
def show(item_id):
    """Ask the Kindle to show this item on its next check-in. Body {"pin": true} keeps it
    there (no rotation) until unpinned."""
    pin = bool((request.get_json(silent=True) or {}).get("pin", False))
    with _lock:
        find(load_index(), item_id)
        d = request_display(item_id, pin)
    return jsonify(d)


@app.post("/api/unpin")
def unpin():
    with _lock:
        d = request_display(None)
    return jsonify(d)


@app.post("/api/item/<item_id>/move")
def move(item_id):
    """Reorder: body {"dir": -1|1}. Kindle cycles in list order."""
    step = int((request.get_json(silent=True) or {}).get("dir", 0))
    with _lock:
        items = load_index()
        i = items.index(find(items, item_id))
        j = i + step
        if 0 <= j < len(items):
            items[i], items[j] = items[j], items[i]
            save_index(items)
    return jsonify(ok=True)


@app.delete("/api/item/<item_id>")
def delete(item_id):
    with _lock:
        items = load_index()
        it = find(items, item_id)
        remove_files(os.path.join(RAW, it["raw"]), os.path.join(OUT, it.get("out", "")),
                     os.path.join(THUMBS, it["raw"] + ".jpg"))
        for c in glob.glob(os.path.join(THUMBS, f"{glob.escape(item_id)}-cover-*.jpg")):
            remove_files(c)
        for pg in it["pages"]:
            remove_files(os.path.join(PAGES, pg), os.path.join(THUMBS, pg + ".jpg"))
        items.remove(it)
        save_index(items)
        if load_display().get("show") == item_id:
            request_display(None)
    return jsonify(ok=True)


@app.get("/preview/<name>")
def preview(name):
    return send_from_directory(OUT, name, max_age=31536000)


def kindle_filenames(items: list[dict]) -> list[str]:
    """Display order. The Kindle stores this as playbills/order.txt and cycles through it."""
    return [it["out"] for it in items if it.get("out")]


@app.post("/api/install")
def install():
    """Copy the Kindle scripts, config and images to a USB-mounted Kindle."""
    if not kindle_mounted():
        return jsonify(error=f"No Kindle mounted at {KINDLE_MOUNT}"), 400
    items = load_index()
    pb = os.path.join(KINDLE_MOUNT, "playbills")
    os.makedirs(pb, exist_ok=True)
    for script in ("update_playbill.sh", "install_cron.sh"):
        shutil.copyfile(os.path.join(KINDLE_SCRIPTS, script), os.path.join(pb, script))
    cfg = os.path.join(pb, "config.sh")
    if not os.path.exists(cfg):  # keep the user's edits on reinstall
        shutil.copyfile(os.path.join(KINDLE_SCRIPTS, "config.sh"), cfg)
    with open(cfg) as f:
        text = f.read()
    text = re.sub(r"^SERVER=.*$", f'SERVER="{server_url()}"', text, flags=re.M)
    with open(cfg, "w") as f:
        f.write(text)

    wanted = kindle_filenames(items)
    for f in os.listdir(pb):
        if f.endswith(".png") and f not in wanted:
            os.remove(os.path.join(pb, f))
    for name in wanted:
        dst = os.path.join(pb, name)
        if not os.path.exists(dst):
            shutil.copyfile(os.path.join(OUT, name), dst)
    with open(os.path.join(pb, "order.txt"), "w") as f:
        f.write("".join(n + "\n" for n in wanted))
    subprocess.run(["dot_clean", "-m", pb], capture_output=True)  # drop macOS ._ files
    return jsonify(ok=True, copied=len(wanted), server=server_url())


@app.post("/api/eject")
def eject():
    r = subprocess.run(["diskutil", "eject", KINDLE_MOUNT], capture_output=True, text=True)
    if r.returncode:
        return jsonify(error=(r.stderr or r.stdout).strip()), 400
    return jsonify(ok=True)


# ---------- routes: Kindle pull ----------

@app.get("/kindle/list.txt")
def kindle_list():
    body = "".join(n + "\n" for n in kindle_filenames(load_index()))
    return app.response_class(body, mimetype="text/plain", headers={"Cache-Control": "no-store"})


@app.get("/kindle/command.txt")
def kindle_command():
    """One line: "<seq> <pin 0|1> <filename or ->". The Kindle applies it once per seq."""
    d = load_display()
    name = "-"
    if d.get("show"):
        for it in load_index():
            if it["id"] == d["show"] and it.get("out"):
                name = it["out"]
    body = f"{d['seq']} {1 if d.get('pin') else 0} {name}\n"
    return app.response_class(body, mimetype="text/plain", headers={"Cache-Control": "no-store"})


@app.get("/kindle/showing")
def kindle_showing():
    """The Kindle reports what is on screen after each run."""
    name = request.args.get("name", "")
    if re.fullmatch(r"[\w.-]+\.png", name):
        _kindle_seen.update(showing=name, at=time.time())
    return "ok\n"


@app.get("/kindle/img/<name>")
def kindle_img(name):
    if not re.fullmatch(r"[\w.-]+\.png", name):
        abort(404)
    return send_from_directory(OUT, name)


def prepare() -> None:
    """Create library folders and migrate old items to the current shape."""
    for d in (RAW, PAGES, OUT, THUMBS):
        os.makedirs(d, exist_ok=True)
    with _lock:
        save_index([normalize(it) for it in load_index()])


if __name__ == "__main__":
    prepare()
    print(f"PlayBillDisplay: open http://localhost:{PORT}  (Kindle uses {server_url()})")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
