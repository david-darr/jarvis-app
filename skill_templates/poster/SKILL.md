---
name: poster
description: |
  Build a real poster, flyer, or single-slide announcement as an actual
  .pptx file, using python-pptx via Bash — precise, legible text and layout,
  not an AI-generated image (image models render multi-element poster text
  as garbled nonsense; this doesn't, because the text is real text, not
  pixels). Use when asked to create, design, or make a poster, flyer, or
  single-page announcement/promo graphic.
license: MIT
metadata:
  version: "1.0.0"
---

# Poster: a real, legible poster as a .pptx file

Posters need multiple distinct pieces of text (headline, subheadline, badges,
a call-to-action) to render exactly as written. An AI image generator can't
do that reliably — diffusion models are notoriously bad at rendering more
than a word or two of legible text. This skill sidesteps the problem
entirely: build the poster as a real PowerPoint slide with real text boxes
and real shapes, using `python-pptx`. The output is a genuine `.pptx` file —
opens in PowerPoint, Google Slides, Keynote, LibreOffice — not a picture of
text.

## Before you start

Confirm `python-pptx` is available: `python -c "import pptx"`. If it isn't,
say so plainly rather than guessing at a workaround — it should already be
installed as part of this app's own requirements.

## Steps

1. **Pick a canvas size.** A print poster is usually portrait. Common sizes,
   in inches: US half-page flyer `8.5 x 11`, full poster `18 x 24` or
   `24 x 36`, a social-media-shareable poster `9 x 16`. Ask if genuinely
   ambiguous; otherwise `8.5 x 11` portrait is a safe default for a flyer.

2. **Write a script, don't improvise in the shell.** Save it to a working
   file (e.g. `/tmp/build_poster.py` or the session's workspace) and run it
   with `python`. The core shape:

   ```python
   from pptx import Presentation
   from pptx.util import Inches, Pt, Emu
   from pptx.dml.color import RGBColor
   from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
   from pptx.enum.shapes import MSO_SHAPE

   WIDTH, HEIGHT = Inches(8.5), Inches(11)  # portrait flyer
   prs = Presentation()
   prs.slide_width, prs.slide_height = WIDTH, HEIGHT
   slide = prs.slides.add_slide(prs.slide_layouts[6])  # 6 = fully blank layout

   # Background color: a full-bleed rectangle, since a blank slide's own
   # background fill is fiddly to set directly in python-pptx.
   bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, WIDTH, HEIGHT)
   bg.fill.solid()
   bg.fill.fore_color.rgb = RGBColor(0x0A, 0x0A, 0x0A)
   bg.line.fill.background()  # no border
   bg.shadow.inherit = False

   # A colored banner block behind the headline
   banner = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0), Inches(0.8), WIDTH, Inches(1.6))
   banner.fill.solid()
   banner.fill.fore_color.rgb = RGBColor(0xC8, 0x10, 0x2E)
   banner.line.fill.background()
   banner.shadow.inherit = False

   # Headline text, centered inside the banner
   headline = slide.shapes.add_textbox(Inches(0.3), Inches(0.9), WIDTH - Inches(0.6), Inches(1.4))
   tf = headline.text_frame
   tf.word_wrap = True
   p = tf.paragraphs[0]
   p.alignment = PP_ALIGN.CENTER
   run = p.add_run()
   run.text = "THE PLAYERS SPORTS ACADEMY"
   run.font.size = Pt(40)
   run.font.bold = True
   run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
   run.font.name = "Arial Black"

   prs.save("/tmp/poster.pptx")
   ```

3. **Build up the rest the same way** — one `add_textbox`/`add_shape` call
   per element (subheadline, body copy, a call-to-action banner near the
   bottom, bullet/badge text). Each text box is independent; position with
   `Inches(...)` left/top/width/height. Use `MSO_ANCHOR` on
   `text_frame.vertical_anchor` to center text vertically inside a box.
   Multiple paragraphs in one box: `tf.add_paragraph()` per line, same
   `run`/`font` pattern as above.

4. **Photos, if asked for or genuinely useful:** `slide.shapes.add_picture(path, left, top, width=..., height=...)` — needs a real local image file. If none exists yet, either skip the photo (a clean text/shape-only poster is a completely valid, real deliverable) or ask whether the user wants one generated separately first — do not silently invent a placeholder image path that doesn't exist.

5. **Keep it print-legible.** Headline 36–48pt bold, subheadline 20–28pt,
   body/details 14–18pt. High contrast (light text on a dark or saturated
   background, or the reverse) — this is a poster meant to be read from a
   few feet away, not a dense document.

6. **Save, then call `save_generated_file`** with the real path the script
   wrote to and a short description (e.g. "PSA recruitment poster"). Include
   the exact markdown link that tool returns in your reply, on its own line,
   so the user gets a real download link.

## Notes

- This produces a `.pptx`, not an image. If the user specifically needs a
  flat image (PNG/JPG) rather than an editable file, say plainly that this
  skill doesn't convert to image format (that needs PowerPoint, Keynote, or
  LibreOffice actually installed and running — not bundled with this app) —
  they can export it themselves from PowerPoint/Google Slides after opening
  the file, or ask if they'd rather regenerate as an image instead (a
  different, lower-fidelity-text tradeoff).
- Never fabricate content the user didn't provide or clearly imply (a phone
  number, an address, a date) — leave a clearly-marked placeholder like
  "[ADD LOCATION]" instead of inventing a real-looking fake one.
