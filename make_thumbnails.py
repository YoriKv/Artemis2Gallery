#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pillow>=10",
# ]
# ///
"""Generate JPEG thumbnails for every image in ./photos/ into ./thumbnails/.

- Resumable: skips photos whose thumbnail already exists.
- Parallel: uses a process pool (one worker per CPU by default), since
  decoding/resizing is CPU-bound and releases of the GIL are libjpeg-internal.
- Safe writes: writes to a .part file then atomically renames, so a Ctrl-C
  mid-encode never leaves a half-written thumbnail.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError


SRC_DIR = Path(__file__).parent / "photos"
DST_DIR = Path(__file__).parent / "thumbnails"
DEFAULT_SIZE = 256          # long-edge pixels
DEFAULT_QUALITY = 85
IMG_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


def make_thumb(src: Path, dst: Path, size: int, quality: int) -> tuple[str, str]:
    """Return (status, message). status is 'ok' | 'skip' | 'fail'."""
    if dst.exists() and dst.stat().st_size > 0:
        return ("skip", str(src.name))
    part = dst.with_suffix(dst.suffix + ".part")
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.thumbnail((size, size), Image.Resampling.LANCZOS)
            im.save(part, format="JPEG", quality=quality,
                    optimize=True, progressive=True)
        os.replace(part, dst)
        return ("ok", str(src.name))
    except (UnidentifiedImageError, OSError, ValueError) as e:
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        return ("fail", f"{src.name}: {e}")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--src", default=str(SRC_DIR))
    p.add_argument("--dst", default=str(DST_DIR))
    p.add_argument("--size", type=int, default=DEFAULT_SIZE,
                   help="Long-edge pixel size (default: 256)")
    p.add_argument("--quality", type=int, default=DEFAULT_QUALITY)
    p.add_argument("--workers", type=int, default=os.cpu_count() or 4)
    args = p.parse_args()

    src_dir = Path(args.src)
    dst_dir = Path(args.dst)
    dst_dir.mkdir(parents=True, exist_ok=True)

    sources = sorted(
        f for f in src_dir.iterdir()
        if f.is_file() and f.suffix.lower() in IMG_EXTS
    )
    total = len(sources)
    print(f"Found {total} images in {src_dir}")
    print(f"Writing thumbnails ({args.size}px, q={args.quality}) "
          f"to {dst_dir} using {args.workers} workers")

    # Pre-filter for already-done thumbnails so the "skipped" count is fast and
    # the progress bar reflects actual work.
    todo = []
    skipped = 0
    for src in sources:
        dst = dst_dir / (src.stem + ".jpg")
        if dst.exists() and dst.stat().st_size > 0:
            skipped += 1
        else:
            todo.append((src, dst))
    print(f"  {skipped} already done, {len(todo)} to process")

    completed = failed = 0
    started = time.monotonic()
    last_log = started

    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(make_thumb, src, dst, args.size, args.quality): src
            for src, dst in todo
        }
        try:
            for fut in as_completed(futures):
                status, msg = fut.result()
                if status == "ok":
                    completed += 1
                elif status == "fail":
                    failed += 1
                    print(f"  FAIL {msg}", file=sys.stderr)
                now = time.monotonic()
                if now - last_log >= 2.0 or completed + failed == len(todo):
                    rate = completed / max(now - started, 1e-3)
                    print(
                        f"[{now - started:5.0f}s] {completed}/{len(todo)} "
                        f"done, {failed} failed, {rate:.1f} img/s",
                        flush=True,
                    )
                    last_log = now
        except KeyboardInterrupt:
            print("\nInterrupted -- cancelling pending thumbnails...",
                  file=sys.stderr)
            for f in futures:
                f.cancel()
            raise

    elapsed = time.monotonic() - started
    print(
        f"\nDone in {elapsed:.0f}s -- "
        f"created={completed} skipped={skipped} failed={failed}"
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
