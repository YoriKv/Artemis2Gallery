#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pillow>=10",
#   "imagehash>=4.3",
# ]
# ///
"""Scan ./photos/ and write photos.json -- a database of every photo with
metadata and the relative path to its thumbnail (when one exists).

The JSON has the shape:
  {
    "generated": "2026-05-07T00:00:00Z",
    "photo_dir": "photos",
    "thumbnail_dir": "thumbnails",
    "count": 11364,
    "items": [
      {
        "name":      "ART002-E-30001",
        "photo":     "photos/ART002-E-30001.JPG",
        "thumbnail": "thumbnails/ART002-E-30001.jpg",   // omitted if missing
        "size":      612345,
        "width":     4928,
        "height":    3280,
        "mtime":     1746576230,
        "taken":     "2025:11:18 14:32:07",             // EXIF, when present
        "phash":     "f4c1e0b29a3d7f51"                 // 64-bit perceptual hash, hex
      },
      ...
    ]
  }

Image dimensions and EXIF date are extracted in parallel with a process pool
since each Pillow open requires reading the file header.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import imagehash
from PIL import Image, UnidentifiedImageError


ROOT = Path(__file__).parent
DEFAULT_PHOTOS = ROOT / "photos"
DEFAULT_THUMBS = ROOT / "thumbnails"
DEFAULT_OUT = ROOT / "photos.json"
DEFAULT_JSONP = ROOT / "photos.js"
DEFAULT_CALLBACK = "loadPhotosManifest"
IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}

def load_phash_cache(path: Path) -> dict[str, str]:
    """Pull the pHash hex strings out of an existing photos.json.

    pHash is the only field expensive enough to be worth caching across runs
    (it requires fully decoding the image and running a DCT). Everything else
    is recomputed from scratch on every run, so we don't try to be clever
    about staleness -- if the cached pHash is wrong, deleting photos.json or
    passing --rebuild forces a recompute.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"warning: couldn't read {path}: {e}", file=sys.stderr)
        return {}
    cache: dict[str, str] = {}
    for item in data.get("items", []):
        if isinstance(item, dict):
            name = item.get("name")
            ph = item.get("phash")
            if name and ph:
                cache[name] = ph
    return cache

# EXIF tag IDs we care about.
TAG_IMAGE_DESCRIPTION = 0x010E   # IFD0 ImageDescription -- caption / free text
TAG_MODEL = 0x0110               # IFD0 Model -- camera body model name
TAG_EXIF_IFD = 0x8769            # pointer to ExifIFD
TAG_DATETIME_ORIGINAL = 0x9003   # ExifIFD DateTimeOriginal -- shutter time
TAG_DATETIME_DIGITIZED = 0x9004  # ExifIFD DateTimeDigitized -- also capture time
# IFD0 DateTime (306) is intentionally not consulted: for the Artemis II set,
# that field carries the JSC ground-processing timestamp (days to weeks after
# capture), which would scramble any time-proximity grouping.

# The four Artemis II crew members. The JSC photo descriptions name the
# shooter inline -- e.g. "FD06_fd6 Lunar Flyby Wiseman" or
# "FD05_Returned_0021_Z9_019_Koch" -- so we just substring-match. Word
# boundaries keep us from matching the names inside larger tokens, but the
# names themselves are uncommon enough that false positives are unlikely.
PHOTOGRAPHERS = ("Koch", "Glover", "Hansen", "Wiseman")
PHOTOGRAPHER_RE = re.compile(
    r"\b(" + "|".join(PHOTOGRAPHERS) + r")\b", re.IGNORECASE,
)


def extract_one(args: tuple[str, str, str, str]) -> dict:
    """Worker: returns a metadata dict for a single photo path.

    Runs in a subprocess, so arguments must be picklable -- hence strings.
    `cached_phash` is the previously computed pHash for this photo (empty
    string if there isn't one); when present we skip the DCT/resize step.
    """
    photo_path_s, photo_rel_s, thumb_rel_s, cached_phash = args
    photo_path = Path(photo_path_s)
    record: dict = {
        "name": photo_path.stem,
        "photo": photo_rel_s,
    }
    if thumb_rel_s:
        record["thumbnail"] = thumb_rel_s

    try:
        st = photo_path.stat()
        record["size"] = st.st_size
        record["mtime"] = int(st.st_mtime)
    except OSError:
        pass

    try:
        with Image.open(photo_path) as im:
            record["width"], record["height"] = im.size
            if cached_phash:
                # The image bytes haven't changed often between runs, and
                # pHash dominates per-photo runtime, so reuse the prior value
                # whenever the previous manifest had one.
                record["phash"] = cached_phash
            else:
                try:
                    # 64-bit perceptual hash (DCT-based). Resilient to
                    # scaling, mild color/quality changes -- handy for
                    # finding visually similar/duplicate frames in the
                    # gallery later. Stored as a 16-char hex string.
                    record["phash"] = str(imagehash.phash(im))
                except Exception:
                    pass
            try:
                exif = im.getexif()
                # ImageDescription lives at IFD0 (top level), not inside the
                # ExifIFD sub-block. JSC writes a free-form caption here that
                # typically embeds the shooter's surname.
                desc = exif.get(TAG_IMAGE_DESCRIPTION)
                if desc:
                    if isinstance(desc, bytes):
                        try: desc = desc.decode("utf-8", errors="replace")
                        except Exception: desc = repr(desc)
                    desc = str(desc).strip("\x00 \t\r\n")
                    if desc:
                        record["description"] = desc
                        m = PHOTOGRAPHER_RE.search(desc)
                        if m:
                            # Canonicalize to title-case so downstream code
                            # can compare with == without re-normalizing.
                            record["photographer"] = m.group(1).capitalize()
                # Camera body model. EXIF stores it all-caps for many makers
                # ("NIKON D5", "NIKON Z 9"); title-case gives a friendlier
                # display string while still grouping cleanly by ==.
                model = exif.get(TAG_MODEL)
                if model:
                    if isinstance(model, bytes):
                        try: model = model.decode("utf-8", errors="replace")
                        except Exception: model = repr(model)
                    model = str(model).strip("\x00 \t\r\n")
                    if model:
                        record["camera"] = model.title()
            except Exception:
                pass
            try:
                exif = im.getexif()
                dt = None
                if exif:
                    # Prefer DateTimeOriginal (when the shutter fired); fall
                    # back to DateTimeDigitized (when the file was first
                    # written, still capture-side). Both come from the
                    # ExifIFD. We do NOT fall back to IFD0 DateTime because
                    # for these JSC-processed images that's the ground-side
                    # ingest time, not the capture time.
                    try:
                        ifd = exif.get_ifd(TAG_EXIF_IFD)
                        dt = (ifd.get(TAG_DATETIME_ORIGINAL)
                              or ifd.get(TAG_DATETIME_DIGITIZED))
                    except Exception:
                        dt = None
                if dt:
                    record["taken"] = str(dt).strip("\x00 ")
            except Exception:
                pass
    except (UnidentifiedImageError, OSError, ValueError):
        # Couldn't decode header -- still emit the record with file-stat data.
        pass

    return record


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--photos", default=str(DEFAULT_PHOTOS))
    p.add_argument("--thumbs", default=str(DEFAULT_THUMBS))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--jsonp", default=str(DEFAULT_JSONP),
                   help="Path to a JSONP-wrapped copy of the manifest "
                        "(loadable from gallery.html via <script src>). "
                        "Pass empty string to skip.")
    p.add_argument("--callback", default=DEFAULT_CALLBACK,
                   help="Name of the global function the JSONP file calls "
                        f"with the manifest object (default: {DEFAULT_CALLBACK})")
    p.add_argument("--no-jsonp", action="store_true",
                   help="Skip writing the JSONP wrapper")
    p.add_argument("--rebuild", action="store_true",
                   help="Ignore any cached pHash values from photos.json and "
                        "recompute pHash for every photo. Other fields are "
                        "always recomputed; this only matters when you want "
                        "to invalidate the pHash cache.")
    p.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    p.add_argument("--pretty", action="store_true",
                   help="Pretty-print the JSON (larger file)")
    args = p.parse_args()

    photos_dir = Path(args.photos).resolve()
    thumbs_dir = Path(args.thumbs).resolve()
    out_path = Path(args.out)
    base = out_path.parent.resolve()

    if not photos_dir.is_dir():
        print(f"error: {photos_dir} is not a directory", file=sys.stderr)
        return 1

    photos = sorted(
        f for f in photos_dir.iterdir()
        if f.is_file() and f.suffix.lower() in IMG_EXTS
    )
    print(f"Scanning {len(photos)} photos in {photos_dir}")

    # Pre-compute thumbnail availability using a stem lookup -- one stat per
    # thumbnail, not per photo, so unmatched photos cost nothing.
    thumb_by_stem: dict[str, Path] = {}
    if thumbs_dir.is_dir():
        for f in thumbs_dir.iterdir():
            if f.is_file() and f.suffix.lower() in IMG_EXTS:
                thumb_by_stem[f.stem] = f
    print(f"  {len(thumb_by_stem)} thumbnails available in {thumbs_dir}")

    def rel(path: Path) -> str:
        # Always emit forward-slash paths so the JSON is portable across
        # platforms / web servers.
        try:
            return path.resolve().relative_to(base).as_posix()
        except ValueError:
            return path.as_posix()

    phash_cache: dict[str, str] = {}
    if not args.rebuild:
        phash_cache = load_phash_cache(out_path)
        if phash_cache:
            print(f"  loaded {len(phash_cache)} cached pHash values from "
                  f"{out_path}")

    # Every photo goes through the worker; each work item carries either the
    # cached pHash (skip DCT) or an empty string (compute fresh).
    work: list[tuple[str, str, str, str]] = []
    cache_hits = 0
    for photo in photos:
        thumb = thumb_by_stem.get(photo.stem)
        thumb_rel_s = rel(thumb) if thumb else ""
        cached = phash_cache.get(photo.stem, "")
        if cached:
            cache_hits += 1
        work.append((str(photo), rel(photo), thumb_rel_s, cached))

    cache_misses = len(work) - cache_hits
    print(f"  {len(work)} photos to process "
          f"({cache_hits} pHash cached, {cache_misses} need pHash compute)")

    items: list[dict] = []
    started = time.monotonic()
    last_log = started

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        # chunksize matters here -- with thousands of tiny tasks, a chunksize
        # of 1 spends most of its time in IPC.
        chunksize = max(1, len(work) // (args.workers * 8))
        for record in pool.map(extract_one, work, chunksize=chunksize):
            items.append(record)
            now = time.monotonic()
            if now - last_log >= 2.0:
                rate = len(items) / max(now - started, 1e-3)
                print(f"  {len(items)}/{len(work)} ({rate:.0f}/s)", flush=True)
                last_log = now

    # Sort by name for stable output.
    items.sort(key=lambda r: r["name"])

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "photo_dir": rel(photos_dir),
        "thumbnail_dir": rel(thumbs_dir) if thumbs_dir.exists() else None,
        "count": len(items),
        "items": items,
    }

    minified = json.dumps(payload, separators=(",", ":"))
    if args.pretty:
        out_path.write_text(json.dumps(payload, indent=2))
    else:
        out_path.write_text(minified)

    elapsed = time.monotonic() - started
    size_mb = out_path.stat().st_size / (1024 * 1024)
    with_thumb = sum(1 for r in items if "thumbnail" in r)
    with_dims = sum(1 for r in items if "width" in r)
    with_taken = sum(1 for r in items if "taken" in r)
    with_phash = sum(1 for r in items if "phash" in r)
    with_desc = sum(1 for r in items if "description" in r)
    by_photog: dict[str, int] = {}
    by_camera: dict[str, int] = {}
    for r in items:
        p = r.get("photographer")
        if p:
            by_photog[p] = by_photog.get(p, 0) + 1
        c = r.get("camera")
        if c:
            by_camera[c] = by_camera.get(c, 0) + 1
    with_photog = sum(by_photog.values())
    with_camera = sum(by_camera.values())
    photog_breakdown = ", ".join(
        f"{name}={by_photog.get(name, 0)}" for name in PHOTOGRAPHERS
    )
    camera_breakdown = ", ".join(
        f"{name}={count}" for name, count in sorted(by_camera.items())
    ) or "none"
    print(
        f"\nWrote {out_path} ({size_mb:.2f} MB) in {elapsed:.1f}s\n"
        f"  items={len(items)}  with_thumbnail={with_thumb}  "
        f"with_dims={with_dims}  with_taken={with_taken}  "
        f"with_phash={with_phash}\n"
        f"  with_description={with_desc}  with_photographer={with_photog} "
        f"({photog_breakdown})\n"
        f"  with_camera={with_camera} ({camera_breakdown})"
    )

    if args.jsonp and not args.no_jsonp:
        jsonp_path = Path(args.jsonp)
        # The wrapper is a single function call. Browsers parse this as a
        # normal script (no JSON.parse needed) and the callback runs at
        # script-load time, so the data is available before any subsequent
        # <script> tag in the document.
        jsonp_path.write_text(f"{args.callback}({minified});\n")
        j_mb = jsonp_path.stat().st_size / (1024 * 1024)
        print(f"  wrote {jsonp_path} ({j_mb:.2f} MB) "
              f"-- load with <script src=\"{jsonp_path.name}\">")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
