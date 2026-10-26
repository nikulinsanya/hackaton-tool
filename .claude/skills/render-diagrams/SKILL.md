---
name: render-diagrams
description: Render the README diagrams (docs/how-it-works.html to docs/images/*.png) and check every image for layout problems. Use when asked to render, regenerate, refresh or update the diagrams, images or screenshots in the README, or after docs/how-it-works.html changed.
allowed-tools: Bash(python3 tools/render-diagrams.py), Read
---

# Render the README diagrams

1. From the repo root run `python3 tools/render-diagrams.py`. It needs Google Chrome; if Chrome is
   installed elsewhere, run it with `CHROME=/path/to/chrome`. It prints one PNG path per figure.
2. Look at every image it printed (Read shows PNGs). Check for text touching or crossing a box edge,
   labels overlapping arrows or other labels, and anything cut off at the image edges.
3. If something collides, fix the coordinates of that figure's SVG in `docs/how-it-works.html`
   (widen a box, shorten a label, move an arrow), run the script again and look again.
4. Image names come from the section headings. If a figure was added, removed or its heading
   renamed, update the image links and explanations in `README.md` ("How it works") to match, and
   delete images that no longer belong to a figure.
5. Tell the user in one or two lines which images changed and what you fixed. Don't commit unless
   they ask.
