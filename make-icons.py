#!/usr/bin/env python3
"""Generate one 512x512 feed icon per feed in a deployment's config.json.

Design: dark rounded tile, one hue per feed (by position in the feed list), the
feed's short label as the dominant mark, and an accent ring + bar. Text only:
it stays legible at the 16-24 px a feed picker renders, and it needs no artwork.

Usage:
  python3 make-icons.py [config.json] [outdir]     # default: ./config.json ./icons

Writes <outdir>/<rkey>.png. publish_feeds.py picks those files up by convention
and uploads them as the generator record's `avatar`.
"""
import json
import os
import subprocess
import sys

# Suffix -> label. The suffix is the rkey with the deployment prefix removed, so
# the same table serves every deployment of the shared engine.
LABELS = {
    "4h": "4h", "24h": "24h", "3d": "3d", "week": "W", "2w": "2W",
    "1m": "1M", "4m": "4M", "trending": "TR", "foryou": "FY", "general": "G",
    "small": "S", "ml": "ML", "focused": "F", "es-hoy": "ES", "es-semana": "7d",
}
HUES = [206, 262, 330, 18, 44, 160, 186, 220, 240, 286, 306, 128, 344, 96]
FONT = "Liberation Sans, DejaVu Sans, sans-serif"


def label_for(rkey, display_name):
    suffix = rkey.split("-", 1)[1] if "-" in rkey else rkey
    if suffix in LABELS:
        return LABELS[suffix]
    words = [w for w in (display_name or rkey).replace("(", " ").split() if w]
    return "".join(w[0] for w in words[:3]).upper() or rkey[:2].upper()


def svg(label, hue, size):
    fg = "#f7f8fc"
    bg1 = "hsl(%d, 52%%, 14%%)" % hue
    bg2 = "hsl(%d, 70%%, 34%%)" % hue
    accent = "hsl(%d, 90%%, 66%%)" % hue
    fs = 200 if len(label) <= 2 else (176 if len(label) == 3 else 150)
    y = 320 if len(label) <= 2 else 315
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 512 512">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{bg1}"/>
      <stop offset="100%" stop-color="{bg2}"/>
    </linearGradient>
  </defs>
  <rect x="0" y="0" width="512" height="512" rx="110" fill="url(#g)"/>
  <circle cx="256" cy="248" r="196" fill="#05070c" fill-opacity="0.30"/>
  <circle cx="256" cy="248" r="196" fill="none" stroke="{accent}"
          stroke-opacity="0.55" stroke-width="14"/>
  <text x="256" y="{y}" text-anchor="middle" font-family="{FONT}"
        font-weight="bold" font-size="{fs}" fill="{fg}"
        letter-spacing="1">{label}</text>
  <rect x="196" y="356" width="120" height="12" rx="6" fill="{accent}"/>
</svg>
"""


def main(argv):
    cfg_path = argv[0] if argv else "config.json"
    outdir = argv[1] if len(argv) > 1 else "icons"
    with open(cfg_path, encoding="utf-8") as f:
        feeds = json.load(f)["feeds"]
    os.makedirs(outdir, exist_ok=True)
    for i, f in enumerate(feeds):
        rkey = f["rkey"]
        label = label_for(rkey, f.get("display_name"))
        raw = os.path.join(outdir, rkey + ".svg")
        png = os.path.join(outdir, rkey + ".png")
        with open(raw, "w", encoding="utf-8") as fh:
            fh.write(svg(label, HUES[i % len(HUES)], 512))
        subprocess.run(["rsvg-convert", "-w", "512", "-h", "512",
                        "-o", png, raw], check=True)
        os.unlink(raw)
        print("%-22s %-4s %6d B" % (rkey, label, os.path.getsize(png)))


if __name__ == "__main__":
    main(sys.argv[1:])
