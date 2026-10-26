#!/usr/bin/env python3
"""
Render the diagrams in docs/how-it-works.html to PNGs in docs/images/ for the README.

    python3 tools/render-diagrams.py

Needs Google Chrome (or set CHROME=/path/to/chrome). Stdlib only. Each <figure> becomes
one image, named after its section heading, in the page's light theme at 2x scale.
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "docs", "how-it-works.html")
OUT = os.path.join(ROOT, "docs", "images")
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
PAD = 24


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def main():
    page = open(SRC, encoding="utf-8").read()
    head = page[:page.index("<main")]  # <title>, font links and the page <style>
    sections = re.findall(r"<h2>(.*?)</h2>.*?(<svg.*?</svg>)", page, re.S)
    os.makedirs(OUT, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix="diagrams-")
    for n, (title, svg) in enumerate(sections, 1):
        w, h = [float(x) for x in re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', svg).groups()]
        html = ('<!doctype html><html data-theme="light"><head><meta charset="utf-8">{}'
                '<style>html,body{{margin:0;background:#FFFFFF}} .shot{{padding:{}px;width:{}px}}'
                '.shot svg{{display:block;width:{}px;height:auto}}</style></head>'
                '<body><div class="shot">{}</div></body></html>').format(head, PAD, w, w, svg)
        src = os.path.join(tmp, "{}.html".format(n))
        with open(src, "w", encoding="utf-8") as f:
            f.write(html)
        name = "{}-{}.png".format(n, slug(re.sub(r"<.*?>", "", title)))
        subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        "--force-device-scale-factor=2", "--virtual-time-budget=4000",
                        "--window-size={},{}".format(int(w + 2 * PAD), int(h + 2 * PAD)),
                        "--screenshot={}".format(os.path.join(OUT, name)), "file://" + src],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("docs/images/" + name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
