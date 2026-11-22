"""Read text from Playbill photos with macOS Vision (on-device, nothing leaves the Mac)."""
from __future__ import annotations

import contextlib
import math
import os
import re
import tempfile

try:
    import Vision
    from Foundation import NSURL

    AVAILABLE = True
except ImportError:  # not macOS, or pyobjc missing
    AVAILABLE = False


def read_boxes(path: str) -> list[dict]:
    """Text boxes: {"text", "x", "y", "w", "h"} in 0..1 coords, y measured from the TOP."""
    if not AVAILABLE:
        raise RuntimeError("Text recognition needs macOS with pyobjc-framework-Vision")
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
    req.setUsesLanguageCorrection_(False)  # names aren't dictionary words
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(path), None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        raise RuntimeError(str(err))
    boxes = []
    for obs in req.results() or []:
        cand = obs.topCandidates_(1)
        if not cand:
            continue
        b = obs.boundingBox()
        boxes.append({
            "text": str(cand[0].string()).strip(),
            "x": b.origin.x, "w": b.size.width, "h": b.size.height,
            "y": 1 - b.origin.y - b.size.height,
        })
    return [b for b in boxes if b["text"]]


@contextlib.contextmanager
def _upright(path: str, crop=None, rotation: int = 0):
    """Yield (jpeg_path, aspect) of the photo with EXIF rotation applied, optionally cut
    to the Playbill (see imaging.apply_crop) and rotated, size capped, so Vision sees
    what the user sees (iPhone photos are often stored sideways)."""
    import imaging  # local import: imaging pulls in OpenCV

    img = imaging.open_photo(path)
    if crop is not None:
        img = imaging.apply_crop(img, crop)
    if rotation:
        img = img.rotate(-rotation, expand=True)
    img.thumbnail((3000, 3000))
    fd, tmp = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        img.save(tmp, "JPEG", quality=92)
        yield tmp, img.width / img.height
    finally:
        os.remove(tmp)


def _rows(boxes: list[dict], slope: float = 0.0) -> list[list[dict]]:
    """Group boxes into visual rows by vertical overlap, each row sorted left to right.
    `slope` corrects for a crooked photo: row height drifts by slope per unit of x."""
    def mid_of(b):
        return b["y"] + b["h"] / 2 - slope * (b["x"] + b["w"] / 2 - 0.5)

    rows: list[list[dict]] = []
    for b in sorted(boxes, key=mid_of):
        mid = mid_of(b)
        if rows:
            last = rows[-1]
            lmid = sum(mid_of(x) for x in last) / len(last)
            lh = max(x["h"] for x in last)
            if abs(mid - lmid) < 0.6 * max(lh, b["h"]):
                last.append(b)
                continue
        rows.append([b])
    return [sorted(r, key=lambda b: b["x"]) for r in rows]


def _straight_rows(boxes: list[dict], aspect: float) -> list[list[dict]]:
    """Try tilts up to ±6° and keep the grouping with the fewest rows (most role/actor pairs)."""
    best = None
    for tenth in sorted(range(-60, 61, 5), key=abs):  # smallest tilt wins ties
        slope = math.tan(math.radians(tenth / 10)) * aspect  # normalized-y per normalized-x
        rows = _rows(boxes, slope)
        if best is None or len(rows) < len(best):
            best = rows
    return best or []


_LEADER = re.compile(r"\s*(?:[.…·•_]\s*){3,}\s*")  # dot leaders: "Hamilton ........ Lin-Manuel"
_TRAIL = re.compile(r"[\s.\u2026·•_]+$")  # leftover leader dots at the end of a block


def cast_lines(path: str) -> list[str]:
    """Read a cast page into lines like "Alexander Hamilton — Lin-Manuel Miranda".

    Playbill cast pages put role and actor on one row (often joined by dot leaders);
    two text blocks on one row become "left — right". Everything else is kept as-is
    so the user can fix it up.
    """
    lines = []
    with _upright(path) as (jpg, aspect):
        boxes = read_boxes(jpg)
    for row in _straight_rows(boxes, aspect):
        parts = [_TRAIL.sub("", _LEADER.sub(" — ", b["text"])).strip(" —") for b in row]
        parts = [p for p in parts if p]
        if not parts:
            continue
        line = " — ".join(parts)
        line = re.sub(r"\s+—\s+", " — ", line)
        lines.append(line)
    return lines


_THEATRE = re.compile(r"\b(THEATRES?|THEATERS?|PLAYHOUSE|HALL|CENTER|CENTRE|STUDIOS?|STAGES?)\b", re.I)
_SKIP = re.compile(r"^(PLAYBILL\W*|®|\W*)$", re.I)


def cover_info(path: str, crop="auto", rotation: int = 0) -> dict:
    """Best guess at {"show", "theatre"} from a cover photo. Either may be ''.
    The title is the biggest lettering, joining lines of similar size top to bottom
    ("THE PLAY" / "THAT GOES" / "WRONG")."""
    with _upright(path, crop, rotation) as (jpg, _):
        boxes = [b for b in read_boxes(jpg) if not _SKIP.match(b["text"])]
    theatre = next((b["text"] for b in boxes if _THEATRE.search(b["text"])), "")
    rest = [b for b in boxes if b["text"] != theatre and len(b["text"]) > 1]
    if not rest:
        return {"show": "", "theatre": _title(theatre)}
    tallest = max(b["h"] for b in rest)
    big = sorted((b for b in rest if b["h"] >= 0.55 * tallest), key=lambda b: (round(b["y"], 2), b["x"]))
    show = " ".join(b["text"] for b in big[:4])
    return {"show": _title(show), "theatre": _title(theatre)}


def _title(s: str) -> str:
    s = s.strip()
    upper = sum(c.isupper() for c in s)
    lower = sum(c.islower() for c in s)
    return s.title() if upper > lower else s  # "WiCKED" / "HAMILTON" -> "Wicked" / "Hamilton"
