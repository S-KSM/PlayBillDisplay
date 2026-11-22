"""Turn a Playbill photo into a 16-gray e-ink image.

The pipeline is tuned so thin marker strokes (signatures) survive the trip to 16 grays:
  0. crop: find the Playbill's four corners in a phone photo (or use corners the user
     dragged) and warp it flat, so only the Playbill is left
  1. orient, rotate, trim plain borders, scale to fit the screen
  2. ink boost: pixels whose color differs from their local surroundings (marker ink) are
     pushed away from the background's brightness. Plain grayscale loses e.g. blue
     Sharpie on gray, because both have the same brightness. Texture without color is untouched.
  3. local contrast (CLAHE) so a signature over a dark or busy area stands out
  4. brightness (gamma), global contrast, unsharp mask to crisp up strokes
  5. quantize to the panel's 16 grays, with or without Floyd-Steinberg dithering
"""
from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps

try:  # lets Pillow open iPhone HEIC photos
    from pillow_heif import register_heif_opener

    register_heif_opener()
except ImportError:
    pass

# Presets shown in the UI. Every key is also a slider/toggle there.
PRESETS: dict[str, dict] = {
    "natural": {"ink": 0.0, "detail": 0.0, "brightness": 0.0, "contrast": 1.2, "sharpness": 0.5,
                "dither": True},
    "signatures": {"ink": 1.5, "detail": 0.8, "brightness": 0.0, "contrast": 1.15, "sharpness": 1.2,
                   "dither": True},
    "high_contrast": {"ink": 1.0, "detail": 1.0, "brightness": 0.0, "contrast": 1.6, "sharpness": 1.0,
                      "dither": False},
}
# crop: "auto" (detect the Playbill), "none" (whole photo), or 4 [x, y] corners in 0..1
# of the upright photo, clockwise from top-left.
DEFAULT = {"preset": "signatures", "rotation": 0, "crop": "auto", **PRESETS["signatures"]}

# The panel shows 16 evenly spaced grays: 0, 17, 34 ... 255.
_PALETTE = Image.new("P", (1, 1))
_PALETTE.putpalette([i * 17 for i in range(16) for _ in range(3)] + [0] * (768 - 48))


def settings_for(item: dict) -> dict:
    """Item's enhancement settings with defaults filled in (old items only had 'rotation')."""
    s = dict(DEFAULT)
    s["rotation"] = item.get("rotation", 0)
    if "enhance" in item and "crop" not in item["enhance"]:
        s["crop"] = "none"  # made before auto-crop existed; don't change how it looks
    s.update(item.get("enhance") or {})
    return s


def clean_settings(raw: dict) -> dict:
    """Validate settings coming from the browser."""
    s = dict(DEFAULT)
    for k, v in (raw or {}).items():
        if k not in s:
            continue
        if k == "crop":
            s[k] = _clean_crop(v)
            continue
        if isinstance(s[k], bool):
            s[k] = bool(v)
        elif k == "preset":
            s[k] = v if v in PRESETS or v == "custom" else "custom"
        elif k == "rotation":
            s[k] = int(v) % 360 // 90 * 90
        else:
            s[k] = float(v)
    s["ink"] = min(max(s["ink"], 0.0), 3.0)
    s["detail"] = min(max(s["detail"], 0.0), 5.0)
    s["brightness"] = min(max(s["brightness"], -1.0), 1.0)
    s["contrast"] = min(max(s["contrast"], 0.5), 2.5)
    s["sharpness"] = min(max(s["sharpness"], 0.0), 3.0)
    return s


def _clean_crop(v):
    if v in ("auto", "none"):
        return v
    try:
        pts = [[min(max(float(x), 0.0), 1.0), min(max(float(y), 0.0), 1.0)] for x, y in v]
    except (TypeError, ValueError):
        return "auto"
    return pts if len(pts) == 4 else "auto"


def _order(pts: np.ndarray) -> np.ndarray:
    """Order 4 points clockwise from top-left."""
    pts = pts.reshape(4, 2).astype(np.float32)
    c = pts.mean(axis=0)
    pts = pts[np.argsort(np.arctan2(pts[:, 1] - c[1], pts[:, 0] - c[0]))]  # clockwise (y down)
    tl = np.argmin(pts.sum(axis=1))
    return np.roll(pts, -tl, axis=0)


def _quad_from_contour(cnt: np.ndarray) -> np.ndarray | None:
    hull = cv2.convexHull(cnt)
    peri = cv2.arcLength(hull, True)
    for eps in (0.01, 0.02, 0.03, 0.05, 0.08):
        approx = cv2.approxPolyDP(hull, eps * peri, True)
        if len(approx) == 4:
            return approx.reshape(4, 2).astype(np.float32)
    return None


def _edge_contrast(gray: np.ndarray, quad: np.ndarray) -> float:
    """Mean brightness difference just inside vs just outside the quad's outline."""
    h, w = gray.shape
    band = max(3, int(min(h, w) * 0.012))
    m = np.zeros((h, w), np.uint8)
    cv2.fillConvexPoly(m, quad.astype(np.int32), 255)
    k = np.ones((2 * band + 1, 2 * band + 1), np.uint8)
    inner = cv2.subtract(m, cv2.erode(m, k))
    outer = cv2.subtract(cv2.dilate(m, k), m)
    if not inner.any() or not outer.any():
        return 0.0
    return abs(float(gray[inner > 0].mean()) - float(gray[outer > 0].mean()))


PLAYBILL_RATIO = 5.375 / 8.5  # width / height of a standard Playbill


def _side_lengths(q: np.ndarray) -> tuple[float, float]:
    tl, tr, br, bl = q
    w = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))
    h = max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))
    return w, h


def _edge_support(edges: np.ndarray, quad: np.ndarray) -> float:
    """Fraction of the quad's outline that lies on (or right next to) a detected edge.
    Uses the weakest side, so a side running across empty table sinks the score."""
    h, w = edges.shape
    t = np.linspace(0, 1, 120)[:, None]
    worst = 1.0
    for i in range(4):
        a, b = quad[i], quad[(i + 1) % 4]
        pts = a + (b - a) * t
        xs = np.clip(pts[:, 0].round().astype(int), 0, w - 1)
        ys = np.clip(pts[:, 1].round().astype(int), 0, h - 1)
        worst = min(worst, float((edges[ys, xs] > 0).mean()))
    return worst


def _line_quads(edges: np.ndarray) -> list[np.ndarray]:
    """Candidate quads from long straight edges: every pair of near-horizontal lines
    crossed with every pair of near-vertical ones."""
    h, w = edges.shape
    segs = cv2.HoughLinesP(edges, 1, np.pi / 360, threshold=60,
                           minLineLength=int(0.2 * min(w, h)), maxLineGap=int(0.03 * max(w, h)))
    if segs is None:
        return []
    horiz, vert = [], []
    for x1, y1, x2, y2 in segs.reshape(-1, 4).astype(np.float32):
        ang = np.degrees(np.arctan2(y2 - y1, x2 - x1)) % 180
        length = np.hypot(x2 - x1, y2 - y1)
        line = (np.float32([x1, y1]), np.float32([x2, y2]), length)
        if ang < 30 or ang > 150:
            horiz.append(line)
        elif 60 < ang < 120:
            vert.append(line)

    def dedupe(lines, key):
        """Keep the longest line per position (parallel duplicates are the same edge)."""
        out = []
        for ln in sorted(lines, key=lambda l: -l[2]):
            if all(abs(key(ln) - key(o)) > 0.04 * (h if key is ymid else w) for o in out):
                out.append(ln)
        return out[:8]

    def ymid(l): return (l[0][1] + l[1][1]) / 2
    def xmid(l): return (l[0][0] + l[1][0]) / 2
    horiz, vert = dedupe(horiz, ymid), dedupe(vert, xmid)

    def cross(l1, l2):
        p, r = l1[0], l1[1] - l1[0]
        q, s_ = l2[0], l2[1] - l2[0]
        den = r[0] * s_[1] - r[1] * s_[0]
        if abs(den) < 1e-6:
            return None
        u = ((q[0] - p[0]) * s_[1] - (q[1] - p[1]) * s_[0]) / den
        return p + u * r

    quads = []
    slack = 0.03 * max(w, h)
    for i in range(len(horiz)):
        for j in range(i + 1, len(horiz)):
            top, bot = sorted((horiz[i], horiz[j]), key=ymid)
            if ymid(bot) - ymid(top) < 0.25 * h:
                continue
            for a in range(len(vert)):
                for b in range(a + 1, len(vert)):
                    left, right = sorted((vert[a], vert[b]), key=xmid)
                    if xmid(right) - xmid(left) < 0.2 * w:
                        continue
                    pts = [cross(top, left), cross(top, right), cross(bot, right), cross(bot, left)]
                    if any(p is None or not (-slack <= p[0] <= w + slack and -slack <= p[1] <= h + slack) for p in pts):
                        continue
                    quads.append(np.float32(pts))
    return quads


def detect_corners(img: Image.Image) -> list[list[float]] | None:
    """Find the Playbill in a photo. Returns 4 [x, y] corners (0..1, clockwise from
    top-left) or None when there's no clear page against a background (e.g. a scan or
    screenshot that is already just the cover)."""
    small = img.copy()
    small.thumbnail((900, 900))
    rgb = np.asarray(small)
    h, w = rgb.shape[:2]
    gray = cv2.GaussianBlur(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), (5, 5), 0)
    # Brightness edges plus color edges: a yellow Playbill banner over a white page behind
    # it is almost invisible in grayscale but obvious in the Lab color channels.
    lab = cv2.GaussianBlur(cv2.cvtColor(rgb, cv2.COLOR_RGB2LAB), (5, 5), 0)
    gray_edges = cv2.Canny(gray, 30, 100)      # for "is the surrounding plain table?"
    edges = gray_edges.copy()                   # for finding and checking the page outline
    for ch in (1, 2):
        edges |= cv2.Canny(lab[..., ch], 12, 30)
    near_edges = cv2.dilate(edges, np.ones((7, 7), np.uint8))  # tolerate a few px of slop

    contours = []
    # A: brightness split (paper vs table), either polarity.
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    k = max(3, min(h, w) // 60) | 1
    for mask in (th, 255 - th):
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2 * k + 1, 2 * k + 1), np.uint8))
        contours += cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0]
    # B: every closed edge outline, inner ones included, so the front sheet's own edge
    # (usually a shadow line) can beat a blob that merged it with a page behind it.
    closed = cv2.morphologyEx(cv2.dilate(edges, np.ones((3, 3), np.uint8)), cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    contours += cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)[0]

    quads = []
    for cnt in contours:
        if cv2.contourArea(cnt) >= 0.12 * w * h:
            q = _quad_from_contour(cnt)
            if q is not None:
                quads.append(q)
    quads += _line_quads(edges)  # C: straight-line hypotheses

    margin = 0.015 * max(w, h)
    best, best_score = None, 0.0
    for quad in quads:
        if not cv2.isContourConvex(quad.astype(np.int32)):
            continue
        quad = _order(quad)
        qa = cv2.contourArea(quad)
        if qa < 0.12 * w * h:
            continue
        # Corners on the frame edge mean the "page" runs off the photo, i.e. this is
        # a scan/screenshot (or a crop of the art), not a Playbill lying on a table.
        on_border = sum(x < margin or y < margin or x > w - margin or y > h - margin for x, y in quad)
        if on_border >= 2:
            continue
        # Outside a real page is table/background: far less detail than the page itself.
        # A rectangle found inside the cover art has busy art all around it.
        inside = np.zeros((h, w), np.uint8)
        cv2.fillConvexPoly(inside, quad.astype(np.int32), 255)
        near = cv2.dilate(inside, np.ones((int(0.03 * min(w, h)) | 1,) * 2, np.uint8))  # skip the page's own edge/shadow
        ring = cv2.dilate(inside, np.ones((int(0.12 * min(w, h)) | 1,) * 2, np.uint8)) - near
        core = cv2.erode(inside, np.ones((int(0.03 * min(w, h)) | 1,) * 2, np.uint8))
        d_in = float((gray_edges[core > 0] > 0).mean()) + 1e-3
        d_out = float((gray_edges[ring > 0] > 0).mean()) if ring.any() else 0.0
        if d_out > 0.6 * d_in:
            continue
        qw, qh = _side_lengths(quad)
        ratio = min(qw, qh) / max(qw, qh)
        shape = float(np.exp(-((ratio - PLAYBILL_RATIO) / 0.09) ** 2))
        support = _edge_support(near_edges, quad)
        contrast = min(_edge_contrast(gray, quad) / 40.0, 1.0)
        score = support * (0.35 + 0.65 * shape) * (0.5 + 0.5 * contrast) * (qa / (w * h)) ** 0.25
        if score > best_score:
            best, best_score = quad, score
    if best is None or best_score < 0.25:
        return None
    return [[float(x) / w, float(y) / h] for x, y in best]


def warp_to(img: Image.Image, corners: list[list[float]]) -> Image.Image:
    """Cut out the quad and flatten it into a rectangle (perspective correction)."""
    w, h = img.size
    src = _order(np.float32([[x * w, y * h] for x, y in corners]))
    tl, tr, br, bl = src
    out_w = int(round(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl))))
    out_h = int(round(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr))))
    if out_w < 20 or out_h < 20:
        return img
    dst = np.float32([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]])
    M = cv2.getPerspectiveTransform(src, dst)
    flat = cv2.warpPerspective(np.asarray(img), M, (out_w, out_h), flags=cv2.INTER_CUBIC,
                               borderMode=cv2.BORDER_REPLICATE)
    # Shave a hair off each edge so no sliver of table survives.
    inset = max(3, int(min(out_w, out_h) * 0.011))
    return Image.fromarray(flat[inset:out_h - inset, inset:out_w - inset])


def apply_crop(img: Image.Image, crop) -> Image.Image:
    if crop == "none":
        return trim(img)
    corners = detect_corners(img) if crop == "auto" else crop
    return warp_to(img, corners) if corners else trim(img)


def open_photo(path: str) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(path)).convert("RGB")


def trim(img: Image.Image) -> Image.Image:
    """Crop away a plain border (e.g. white space around a screenshot), using the corner color."""
    bg = Image.new("RGB", img.size, img.getpixel((0, 0)))
    diff = ImageChops.difference(img, bg).convert("L").point(lambda v: 255 if v > 24 else 0)
    box = diff.getbbox()
    if not box:
        return img
    cropped = img.crop(box)
    if cropped.width * cropped.height < 0.05 * img.width * img.height:  # noise, not a border
        return img
    return cropped


def to_gray(img: Image.Image, ink: float) -> Image.Image:
    """Luminance, plus 'ink boost' (see module doc) when ink > 0."""
    if ink <= 0:
        return ImageOps.grayscale(img)
    lab = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2LAB)  # uint8: L 0-255, a/b offset 128
    k = max(5, (min(img.size) // 40) | 1)                    # odd window, a few stroke widths
    bg = cv2.medianBlur(lab, k).astype(np.float32)
    lab = lab.astype(np.float32)
    d_chroma = np.hypot(lab[..., 1] - bg[..., 1], lab[..., 2] - bg[..., 2])
    d_chroma = np.clip(d_chroma - 6, 0, None)                # ignore sensor noise / jpeg tint
    d_l = lab[..., 0] - bg[..., 0]
    # Push away from the background: keep the ink's own direction if it has one,
    # otherwise darken on light backgrounds and lighten on dark ones.
    direction = np.where(np.abs(d_l) > 6, np.sign(d_l), np.where(bg[..., 0] > 110, -1.0, 1.0))
    L = np.clip(lab[..., 0] + direction * ink * 2.2 * d_chroma, 0, 255).astype(np.uint8)
    return Image.fromarray(L)  # OpenCV's L channel is already scaled to 0-255


def local_contrast(gray: Image.Image, strength: float) -> Image.Image:
    if strength <= 0:
        return gray
    tiles = max(2, round(min(gray.size) / 250))  # big tiles: lift areas, don't amplify grain
    clahe = cv2.createCLAHE(clipLimit=1.0 + strength, tileGridSize=(tiles, tiles))
    out = clahe.apply(np.asarray(gray))
    # Blend so low settings stay subtle.
    a = min(strength / 2.5, 1.0)
    return Image.blend(gray, Image.fromarray(out), a)


def process(img: Image.Image, s: dict, size: tuple[int, int], margin: int) -> Image.Image:
    """Return an 'L' image of exactly `size`, quantized to 16 grays."""
    w, h = size
    img = apply_crop(img, s.get("crop", "auto"))
    if s.get("rotation"):
        img = img.rotate(-s["rotation"], expand=True)  # clockwise
    scale = min((w - 2 * margin) / img.width, (h - 2 * margin) / img.height)
    img = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                     Image.Resampling.LANCZOS)

    gray = to_gray(img, s["ink"])
    gray = ImageOps.autocontrast(gray, cutoff=1)
    gray = local_contrast(gray, s["detail"])
    if s["brightness"]:
        gamma = 2 ** (-s["brightness"])  # +1 -> brighter
        gray = gray.point(lambda v: round(255 * (v / 255) ** gamma))
    gray = ImageEnhance.Contrast(gray).enhance(s["contrast"])
    if s["sharpness"] > 0:
        r = max(1.0, min(w, h) / 600)  # stroke-width-ish radius, scales with output size
        gray = gray.filter(ImageFilter.UnsharpMask(radius=r, percent=int(80 * s["sharpness"]), threshold=2))

    canvas = Image.new("L", (w, h), 255)
    canvas.paste(gray, ((w - gray.width) // 2, (h - gray.height) // 2))
    dither = Image.Dither.FLOYDSTEINBERG if s["dither"] else Image.Dither.NONE
    return canvas.convert("RGB").quantize(palette=_PALETTE, dither=dither).convert("L")


def render_png(path: str, s: dict, size: tuple[int, int], margin: int) -> bytes:
    out = process(open_photo(path), s, size, margin)
    buf = io.BytesIO()
    out.save(buf, "PNG", optimize=True)  # 8-bit grayscale: what eips wants
    return buf.getvalue()


def cover_jpeg(path: str, s: dict, max_side: int = 900) -> bytes:
    """Full-color cover for the library: same crop and rotation as the Kindle image."""
    img = apply_crop(open_photo(path), s.get("crop", "auto"))
    if s.get("rotation"):
        img = img.rotate(-s["rotation"], expand=True)
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=86)
    return buf.getvalue()


def thumbnail_jpeg(path: str, max_side: int = 900) -> bytes:
    img = open_photo(path)
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()
