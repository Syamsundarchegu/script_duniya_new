# import base64
# import io
# import logging
# import os
# import urllib.request
# from typing import Optional

# from PIL import Image, ImageDraw, ImageFont

# log = logging.getLogger(__name__)

# # ── Hardcoded font paths (DejaVu ships with most Linux/Ubuntu installs) ────
# _FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
# _FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# _FONT_BOLD_FALLBACKS = [
#     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
#     "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
# ]
# _FONT_REG_FALLBACKS = [
#     "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
#     "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
# ]

# # ── Font download cache ────────────
# _FONT_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".font_cache")

# _FONT_URLS = {
#     "bold":    "https://raw.githubusercontent.com/googlefonts/roboto/main/src/hinted/Roboto-Bold.ttf",
#     "regular": "https://raw.githubusercontent.com/googlefonts/roboto/main/src/hinted/Roboto-Regular.ttf",
# }

# # ══════════════════════════════════════════════════════════════════════════════
# # INTERNAL HELPERS
# # ══════════════════════════════════════════════════════════════════════════════

# import base64

# import io

# import logging

# import os

# import urllib.request

# import math

# from typing import Optional
 
# from PIL import Image, ImageDraw, ImageFont
 
# log = logging.getLogger(__name__)
 
# # ── Hardcoded font paths (DejaVu ships with most Linux/Ubuntu installs) ────

# _FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# _FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
 
# _FONT_BOLD_FALLBACKS = [

#     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",

#     "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",

# ]

# _FONT_REG_FALLBACKS = [

#     "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",

#     "/usr/share/fonts/truetype/freefont/FreeSans.ttf",

# ]
 
# # ── Font download cache ────────────

# _FONT_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".font_cache")
 
# _FONT_URLS = {

#     "bold":    "https://raw.githubusercontent.com/googlefonts/roboto/main/src/hinted/Roboto-Bold.ttf",

#     "regular": "https://raw.githubusercontent.com/googlefonts/roboto/main/src/hinted/Roboto-Regular.ttf",

# }
 
# # ══════════════════════════════════════════════════════════════════════════════

# # INTERNAL HELPERS

# # ══════════════════════════════════════════════════════════════════════════════
 
# def _ensure_fonts() -> tuple:

#     os.makedirs(_FONT_CACHE_DIR, exist_ok=True)

#     bold_path    = os.path.join(_FONT_CACHE_DIR, "Roboto-Bold.ttf")

#     regular_path = os.path.join(_FONT_CACHE_DIR, "Roboto-Regular.ttf")
 
#     for path, url in [

#         (bold_path,    _FONT_URLS["bold"]),

#         (regular_path, _FONT_URLS["regular"]),

#     ]:

#         if not os.path.exists(path):

#             try:

#                 log.info(f"Downloading font → {path}")

#                 urllib.request.urlretrieve(url, path)

#                 log.info(f"Font cached: {os.path.basename(path)}")

#             except Exception as e:

#                 log.warning(f"Font download failed ({url}): {e}")

#                 return None, None
 
#     return bold_path, regular_path
 
# def _load_font(primary: str, fallbacks: list, size: int) -> ImageFont.FreeTypeFont:

#     for path in [primary] + fallbacks:

#         if os.path.exists(path):

#             try:

#                 return ImageFont.truetype(path, size)

#             except Exception:

#                 continue
 
#     prefer_bold  = "bold" in primary.lower()

#     bold_path, regular_path = _ensure_fonts()

#     chosen = bold_path if prefer_bold else regular_path
 
#     if chosen and os.path.exists(chosen):

#         try:

#             return ImageFont.truetype(chosen, size)

#         except Exception as e:

#             log.warning(f"Downloaded font load failed: {e}")
 
#     log.warning("No TrueType font found; using Pillow default bitmap font.")

#     return ImageFont.load_default()
 
# def _get_text_width(draw, text, font):

#     try:

#         return draw.textbbox((0, 0), text, font=font)[2]

#     except AttributeError:

#         return draw.textsize(text, font=font)[0]
 
# def _get_text_height(draw, text, font):

#     try:

#         bbox = draw.textbbox((0, 0), text, font=font)

#         return bbox[3] - bbox[1]

#     except AttributeError:

#         return draw.textsize(text, font=font)[1]
 
# def _truncate_to_width(draw, text: str, font, max_width: int) -> str:

#     if not text:

#         return ""

#     if _get_text_width(draw, text, font) <= max_width:

#         return text

#     while len(text) > 0:

#         text = text[:-1]

#         if _get_text_width(draw, text + "...", font) <= max_width:

#             return text + "..."

#     return ""
 
 
# # ══════════════════════════════════════════════════════════════════════════════

# # ARROW DRAWING LOGIC

# # ══════════════════════════════════════════════════════════════════════════════
 
# def _draw_overlay_arrow(img: Image.Image, movement: str):

#     """

#     Draws a highly visible arrow on the original storyboard frame based on the 

#     camera movement direction extracted from the LLM.

#     """

#     if not movement:

#         return

#     m = movement.lower()

#     W, H = img.size

#     draw = ImageDraw.Draw(img)

#     # Scale arrow size relative to image width

#     line_w = max(4, int(W * 0.008))

#     arr_len = max(25, int(W * 0.05))

#     arr_wid = max(20, int(W * 0.04))

#     def draw_arrow(start, end):

#         x1, y1 = start

#         x2, y2 = end

#         dx, dy = x2 - x1, y2 - y1

#         L = math.hypot(dx, dy)

#         if L == 0: return

#         # Unit vectors

#         ux, uy = dx/L, dy/L

#         # Perpendicular vectors

#         px, py = -uy, ux

#         # Calculate arrowhead points

#         x_b, y_b = x2 - arr_len*ux, y2 - arr_len*uy

#         p1 = (x_b + arr_wid/2 * px, y_b + arr_wid/2 * py)

#         p2 = (x_b - arr_wid/2 * px, y_b - arr_wid/2 * py)

#         # 1. Draw Black Outline (slightly larger)

#         draw.line([start, end], fill="black", width=line_w + 6)

#         # Outlined arrowhead points

#         p1_out = (x_b + (arr_wid/2 + 6) * px, y_b + (arr_wid/2 + 6) * py)

#         p2_out = (x_b - (arr_wid/2 + 6) * px, y_b - (arr_wid/2 + 6) * py)

#         x_tip_out = x2 + 6 * ux

#         y_tip_out = y2 + 6 * uy

#         draw.polygon([(x_tip_out, y_tip_out), p1_out, p2_out], fill="black")

#         # 2. Draw White Inner 

#         draw.line([start, (x_b, y_b)], fill="white", width=line_w)

#         draw.polygon([(x2, y2), p1, p2], fill="white")
 
#     # Determine coordinates based on movement text

#     if "→" in movement or "right" in m:

#         draw_arrow((W*0.1, H*0.85), (W*0.9, H*0.85))

#     elif "←" in movement or "left" in m:

#         draw_arrow((W*0.9, H*0.85), (W*0.1, H*0.85))

#     elif "↑" in movement or "up" in m:

#         draw_arrow((W*0.1, H*0.9), (W*0.1, H*0.1))

#     elif "↓" in movement or "down" in m:

#         draw_arrow((W*0.1, H*0.1), (W*0.1, H*0.9))

#     elif "push in" in m or "zoom in" in m or "↗" in movement:

#         draw_arrow((W*0.1, H*0.9), (W*0.35, H*0.65))

#     elif "pull back" in m or "zoom out" in m or "↙" in movement:

#         draw_arrow((W*0.35, H*0.65), (W*0.1, H*0.9))

#     elif "⟳" in movement or "360" in m or "rotate" in m:

#         draw_arrow((W*0.2, H*0.85), (W*0.8, H*0.85))

#         # Add text for 360 over the arrow

#         font_sm = _load_font(_FONT_BOLD, _FONT_BOLD_FALLBACKS, max(20, int(H * 0.035)))

#         draw.text((W*0.48, H*0.80), "360°", font=font_sm, fill="white", stroke_fill="black", stroke_width=2)
 
 
# # ══════════════════════════════════════════════════════════════════════════════

# # PUBLIC API

# # ══════════════════════════════════════════════════════════════════════════════
 
# def stamp_metadata_on_image(b64_data: str, metadata: dict) -> str:

#     try:

#         raw_bytes = base64.b64decode(b64_data)

#         img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")

#     except Exception as e:

#         log.error(f"stamp_metadata_on_image: failed to decode image — {e}")

#         return b64_data
 
#     W, H = img.size
 
#     # ── NEW: Draw the arrow directly on the scene image before adding banner ──

#     movement_notation = metadata.get("movement", "")

#     _draw_overlay_arrow(img, movement_notation)

#     # ──────────────────────────────────────────────────────────────────────────
 
#     # Set font sizes dynamically based on image height

#     font_size_lg = max(28, int(H * 0.045))  

#     font_size_sm = max(20, int(H * 0.032))  

#     pad          = max(15, int(W * 0.015))

#     line_gap     = max(5,  int(H * 0.008))
 
#     font_bold  = _load_font(_FONT_BOLD, _FONT_BOLD_FALLBACKS, font_size_lg)

#     font_small = _load_font(_FONT_REG,  _FONT_REG_FALLBACKS,  font_size_sm)
 
#     scene_no    = str(metadata.get("scene", "")).replace("\n", " ").strip()

#     frame_id    = str(metadata.get("frame_id", "")).replace("\n", " ").strip()

#     location    = str(metadata.get("location", "")).upper().replace("\n", " ").strip()

#     time_of_day = str(metadata.get("time_of_day", "")).upper().replace("\n", " ").strip()

#     shot_type   = str(metadata.get("shot_type", "")).upper().replace("\n", " ").strip()

#     action      = str(metadata.get("action", "")).replace("\n", " ").strip()

#     composition = str(metadata.get("composition", "")).replace("\n", " ").strip()
 
#     temp_img = Image.new("RGB", (1, 1))

#     temp_draw = ImageDraw.Draw(temp_img)
 
#     left_heading = f"SCENE {scene_no}   FRAME {frame_id}".strip()

#     loc_time     = "  ·  ".join(filter(None, [location, time_of_day]))
 
#     max_text_width = W - (pad * 2)

#     left_w = _get_text_width(temp_draw, left_heading, font_bold)

#     available_right_w = W - left_w - (pad * 3) 

#     right_heading = _truncate_to_width(temp_draw, shot_type, font_bold, available_right_w)
 
#     loc_time_disp  = _truncate_to_width(temp_draw, loc_time, font_small, max_text_width)

#     action_disp    = _truncate_to_width(temp_draw, action, font_small, max_text_width)

#     comp_disp      = _truncate_to_width(temp_draw, composition, font_small, max_text_width)
 
#     current_y = pad
 
#     # Row 1 Height

#     row1_h = _get_text_height(temp_draw, left_heading, font_bold)

#     if right_heading:

#         row1_h = max(row1_h, _get_text_height(temp_draw, right_heading, font_bold))

#     current_y += row1_h + line_gap
 
#     # Row 2, 3, 4 Heights

#     if loc_time_disp:

#         current_y += _get_text_height(temp_draw, loc_time_disp, font_small) + line_gap

#     if action_disp:

#         current_y += _get_text_height(temp_draw, action_disp, font_small) + line_gap

#     if comp_disp:

#         current_y += _get_text_height(temp_draw, comp_disp, font_small) + line_gap
 
#     banner_h = int(current_y + pad)
 
#     # ── Draw the actual banner ────────────────────────────────────────────

#     banner = Image.new("RGB", (W, banner_h), color=(255, 255, 255))

#     draw   = ImageDraw.Draw(banner)
 
#     draw_y = pad
 
#     # Row 1

#     draw.text((pad, draw_y), left_heading, font=font_bold, fill=(0, 0, 0))

#     if right_heading:

#         rw = _get_text_width(draw, right_heading, font_bold)

#         draw.text((W - rw - pad, draw_y), right_heading, font=font_bold, fill=(50, 50, 50))

#     draw_y += row1_h + line_gap
 
#     # Row 2

#     if loc_time_disp:

#         draw.text((pad, draw_y), loc_time_disp, font=font_small, fill=(40, 40, 40))

#         draw_y += _get_text_height(draw, loc_time_disp, font_small) + line_gap
 
#     # Row 3

#     if action_disp:

#         draw.text((pad, draw_y), action_disp, font=font_small, fill=(70, 70, 70))

#         draw_y += _get_text_height(draw, action_disp, font_small) + line_gap
 
#     # Row 4

#     if comp_disp:

#         draw.text((pad, draw_y), comp_disp, font=font_small, fill=(100, 100, 100))
 
#     # Borders

#     draw.line([(0, banner_h - 2), (W, banner_h - 2)], fill=(0, 0, 0), width=3)

#     draw.line([(0, 0), (W, 0)], fill=(0, 0, 0), width=3)
 
#     # ── Compose and export ────────────────────────────────────────────────

#     combined = Image.new("RGB", (W, H + banner_h), color=(255, 255, 255))

#     combined.paste(banner, (0, 0))

#     combined.paste(img,    (0, banner_h))
 
#     try:

#         buf = io.BytesIO()

#         combined.save(buf, format="JPEG", quality=85, optimize=True, progressive=True)

#         buf.seek(0)

#         result = base64.b64encode(buf.read()).decode("utf-8")

#         log.info(f"stamp_metadata_on_image: stamped {frame_id} ({W}x{H} → {W}x{H + banner_h}, banner={banner_h}px)")

#         return result

#     except Exception as e:

#         log.error(f"stamp_metadata_on_image: failed to encode output — {e}")

#         return b64_data






import base64
import io
import logging
import os
import urllib.request
import math
from typing import Optional
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# ── Hardcoded font paths (DejaVu ships with most Linux/Ubuntu installs) ────
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_FONT_REG  = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

_FONT_BOLD_FALLBACKS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
]
_FONT_REG_FALLBACKS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]

# ── Font download cache ────────────
_FONT_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".font_cache")

_FONT_URLS = {
    "bold":    "https://raw.githubusercontent.com/googlefonts/roboto/main/src/hinted/Roboto-Bold.ttf",
    "regular": "https://raw.githubusercontent.com/googlefonts/roboto/main/src/hinted/Roboto-Regular.ttf",
}

# ══════════════════════════════════════════════════════════════════════════════
# INTERNAL HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _ensure_fonts() -> tuple:
    os.makedirs(_FONT_CACHE_DIR, exist_ok=True)
    bold_path    = os.path.join(_FONT_CACHE_DIR, "Roboto-Bold.ttf")
    regular_path = os.path.join(_FONT_CACHE_DIR, "Roboto-Regular.ttf")

    for path, url in [
        (bold_path,    _FONT_URLS["bold"]),
        (regular_path, _FONT_URLS["regular"]),
    ]:
        if not os.path.exists(path):
            try:
                log.info(f"Downloading font → {path}")
                urllib.request.urlretrieve(url, path)
                log.info(f"Font cached: {os.path.basename(path)}")
            except Exception as e:
                log.warning(f"Font download failed ({url}): {e}")
                return None, None

    return bold_path, regular_path

def _load_font(primary: str, fallbacks: list, size: int) -> ImageFont.FreeTypeFont:
    for path in [primary] + fallbacks:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    prefer_bold  = "bold" in primary.lower()
    bold_path, regular_path = _ensure_fonts()
    chosen = bold_path if prefer_bold else regular_path

    if chosen and os.path.exists(chosen):
        try:
            return ImageFont.truetype(chosen, size)
        except Exception as e:
            log.warning(f"Downloaded font load failed: {e}")

    log.warning("No TrueType font found; using Pillow default bitmap font.")
    return ImageFont.load_default()

def _get_text_width(draw, text, font):
    try:
        return draw.textbbox((0, 0), text, font=font)[2]
    except AttributeError:
        return draw.textsize(text, font=font)[0]

def _get_text_height(draw, text, font):
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[3] - bbox[1]
    except AttributeError:
        return draw.textsize(text, font=font)[1]

def _truncate_to_width(draw, text: str, font, max_width: int) -> str:
    if not text:
        return ""
    if _get_text_width(draw, text, font) <= max_width:
        return text
    while len(text) > 0:
        text = text[:-1]
        if _get_text_width(draw, text + "...", font) <= max_width:
            return text + "..."
    return ""

# ══════════════════════════════════════════════════════════════════════════════
# NEW: STORYBOARD ARROW DRAWING LOGIC
# ══════════════════════════════════════════════════════════════════════════════

def _draw_arrow_segment(draw, start, end, width, color=(0, 0, 0), halo=(255, 255, 255)):
    """Draws a chunky, polygon-based arrow with a thick white outline to match storyboard styles."""
    x1, y1 = int(start[0]), int(start[1])
    x2, y2 = int(end[0]), int(end[1])

    dx = x2 - x1
    dy = y2 - y1
    length = math.hypot(dx, dy)
    if length == 0: return

    # Calculate unit vector (direction) and perpendicular vector (thickness)
    ux, uy = dx / length, dy / length
    px, py = -uy, ux

    # Chunky proportions for the arrowhead
    head_length = width * 2.5
    head_width = width * 3.5

    # Scale down the head if the arrow itself is extremely short
    if length < head_length:
        head_length = length
        head_width = length * 1.5

    # Calculate the base of the arrowhead triangle
    hx = x2 - head_length * ux
    hy = y2 - head_length * uy

    def draw_poly_arrow(w, hw, hl, fill_color):
        # 1. Tail polygon (the stem of the arrow)
        t1 = (x1 + px * w / 2, y1 + py * w / 2)
        t2 = (x1 - px * w / 2, y1 - py * w / 2)
        t3 = (hx - px * w / 2, hy - py * w / 2)
        t4 = (hx + px * w / 2, hy + py * w / 2)
        
        # 2. Head polygon (the triangle tip)
        h1 = (x2, y2)
        h2 = (hx + px * hw / 2, hy + py * hw / 2)
        h3 = (hx - px * hw / 2, hy - py * hw / 2)

        draw.polygon([t1, t2, t3, t4], fill=fill_color)
        draw.polygon([h1, h2, h3], fill=fill_color)

    # Draw the white halo first (slightly larger to act as an outline)
    if halo:
        pad = max(3, int(width * 0.3))
        draw_poly_arrow(width + pad * 2, head_width + pad * 2, head_length + pad, halo)

    # Draw the inner core (black body)
    draw_poly_arrow(width, head_width, head_length, color)

def _draw_overlay_arrow(img: Image.Image, movement_notation: str):
    """
    Draws storyboard-convention directional arrows onto the generated frame.
    Calculations updated for shorter, wider arrows.
    """
    if not movement_notation:
        return

    W, H = img.size
    d = ImageDraw.Draw(img)
    
    # 1. INCREASE WIDTH: Changed from 0.008 to 0.018 for a thicker base stem
    w = max(10, int(W * 0.018)) 
    
    # 2. DECREASE LENGTH: Define a shorter fixed span and a tighter corner inset
    ins = int(min(W, H) * 0.05)  # 5% inset (pushes arrows closer to the edges)
    span = int(min(W, H) * 0.18) # 18% span (dictates the exact short length of the arrow)

    m = movement_notation.lower()

    if any(k in m for k in ["zoom in", "push in", "dolly in", "↗"]):
        # 4 arrows pointing INWARD
        for s, e in [((ins, ins), (ins + span, ins + span)),
                     ((W - ins, ins), (W - ins - span, ins + span)),
                     ((ins, H - ins), (ins + span, H - ins - span)),
                     ((W - ins, H - ins), (W - ins - span, H - ins - span))]:
            _draw_arrow_segment(d, s, e, w)

    elif any(k in m for k in ["zoom out", "pull back", "dolly out", "↙"]):
        # 4 arrows pointing OUTWARD (Matches your first reference image)
        for s, e in [((ins + span, ins + span), (ins, ins)),
                     ((W - ins - span, ins + span), (W - ins, ins)),
                     ((ins + span, H - ins - span), (ins, H - ins)),
                     ((W - ins - span, H - ins - span), (W - ins, H - ins))]:
            _draw_arrow_segment(d, s, e, w)

    elif any(k in m for k in ["→", "right"]):
        # Centered short arrows for panning
        _draw_arrow_segment(d, (W * .5 - span, H - ins - w*2), (W * .5 + span, H - ins - w*2), w)

    elif any(k in m for k in ["←", "left"]):
        _draw_arrow_segment(d, (W * .5 + span, H - ins - w*2), (W * .5 - span, H - ins - w*2), w)

    elif any(k in m for k in ["↑", "tilt up", "crane up", "upward"]):
        _draw_arrow_segment(d, (ins + w*2, H * .5 + span), (ins + w*2, H * .5 - span), w)

    elif any(k in m for k in ["↓", "tilt down", "crane down", "downward"]):
        _draw_arrow_segment(d, (ins + w*2, H * .5 - span), (ins + w*2, H * .5 + span), w)

    elif any(k in m for k in ["⟳", "360", "rotate", "dolly around"]):
        # Curved 360 degree arrow adjusted for thickness
        bbox = [W * .35, H * .35, W * .65, H * .65]
        d.arc(bbox, 200, 510, fill=(255, 255, 255), width=w + 6) # Halo
        d.arc(bbox, 200, 510, fill=(0, 0, 0), width=w) # Black line
        a = math.radians(510)
        ex = W * .5 + W * .15 * math.cos(a)
        ey = H * .5 + H * .15 * math.sin(a)
        _draw_arrow_segment(d, (ex - 15, ey), (ex, ey + 15), w)

# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ══════════════════════════════════════════════════════════════════════════════

def stamp_metadata_on_image(b64_data: str, metadata: dict) -> str:
    try:
        raw_bytes = base64.b64decode(b64_data)
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")
    except Exception as e:
        log.error(f"stamp_metadata_on_image: failed to decode image — {e}")
        return b64_data

    W, H = img.size

    # ── Draw the arrow directly on the scene image before adding banner ──
    movement_notation = metadata.get("movement", "")
    _draw_overlay_arrow(img, movement_notation)
    # ──────────────────────────────────────────────────────────────────────────

    # Set font sizes dynamically based on image height
    font_size_lg = max(28, int(H * 0.045))  
    font_size_sm = max(20, int(H * 0.032))  
    pad          = max(15, int(W * 0.015))
    line_gap     = max(5,  int(H * 0.008))

    font_bold  = _load_font(_FONT_BOLD, _FONT_BOLD_FALLBACKS, font_size_lg)
    font_small = _load_font(_FONT_REG,  _FONT_REG_FALLBACKS,  font_size_sm)

    scene_no    = str(metadata.get("scene", "")).replace("\n", " ").strip()
    frame_id    = str(metadata.get("frame_id", "")).replace("\n", " ").strip()
    location    = str(metadata.get("location", "")).upper().replace("\n", " ").strip()
    time_of_day = str(metadata.get("time_of_day", "")).upper().replace("\n", " ").strip()
    shot_type   = str(metadata.get("shot_type", "")).upper().replace("\n", " ").strip()
    action      = str(metadata.get("action", "")).replace("\n", " ").strip()
    composition = str(metadata.get("composition", "")).replace("\n", " ").strip()

    temp_img = Image.new("RGB", (1, 1))
    temp_draw = ImageDraw.Draw(temp_img)

    left_heading = f"SCENE {scene_no}   FRAME {frame_id}".strip()
    loc_time     = "  ·  ".join(filter(None, [location, time_of_day]))

    max_text_width = W - (pad * 2)

    left_w = _get_text_width(temp_draw, left_heading, font_bold)
    available_right_w = W - left_w - (pad * 3) 
    right_heading = _truncate_to_width(temp_draw, shot_type, font_bold, available_right_w)

    loc_time_disp  = _truncate_to_width(temp_draw, loc_time, font_small, max_text_width)
    action_disp    = _truncate_to_width(temp_draw, action, font_small, max_text_width)
    comp_disp      = _truncate_to_width(temp_draw, composition, font_small, max_text_width)

    current_y = pad

    # Row 1 Height
    row1_h = _get_text_height(temp_draw, left_heading, font_bold)
    if right_heading:
        row1_h = max(row1_h, _get_text_height(temp_draw, right_heading, font_bold))
    current_y += row1_h + line_gap

    # Row 2, 3, 4 Heights
    if loc_time_disp:
        current_y += _get_text_height(temp_draw, loc_time_disp, font_small) + line_gap
    if action_disp:
        current_y += _get_text_height(temp_draw, action_disp, font_small) + line_gap
    if comp_disp:
        current_y += _get_text_height(temp_draw, comp_disp, font_small) + line_gap

    banner_h = int(current_y + pad)

    # ── Draw the actual banner ────────────────────────────────────────────
    banner = Image.new("RGB", (W, banner_h), color=(255, 255, 255))
    draw   = ImageDraw.Draw(banner)

    draw_y = pad

    # Row 1
    draw.text((pad, draw_y), left_heading, font=font_bold, fill=(0, 0, 0))
    if right_heading:
        rw = _get_text_width(draw, right_heading, font_bold)
        draw.text((W - rw - pad, draw_y), right_heading, font=font_bold, fill=(50, 50, 50))
    draw_y += row1_h + line_gap

    # Row 2
    if loc_time_disp:
        draw.text((pad, draw_y), loc_time_disp, font=font_small, fill=(40, 40, 40))
        draw_y += _get_text_height(draw, loc_time_disp, font_small) + line_gap

    # Row 3
    if action_disp:
        draw.text((pad, draw_y), action_disp, font=font_small, fill=(70, 70, 70))
        draw_y += _get_text_height(draw, action_disp, font_small) + line_gap

    # Row 4
    if comp_disp:
        draw.text((pad, draw_y), comp_disp, font=font_small, fill=(100, 100, 100))

    # Borders
    draw.line([(0, banner_h - 2), (W, banner_h - 2)], fill=(0, 0, 0), width=3)
    draw.line([(0, 0), (W, 0)], fill=(0, 0, 0), width=3)

    # ── Compose and export ────────────────────────────────────────────────
    combined = Image.new("RGB", (W, H + banner_h), color=(255, 255, 255))
    combined.paste(banner, (0, 0))
    combined.paste(img,    (0, banner_h))

    try:
        buf = io.BytesIO()
        combined.save(buf, format="JPEG", quality=85, optimize=True, progressive=True)
        buf.seek(0)
        result = base64.b64encode(buf.read()).decode("utf-8")
        log.info(f"stamp_metadata_on_image: stamped {frame_id} ({W}x{H} → {W}x{H + banner_h}, banner={banner_h}px)")
        return result
    except Exception as e:
        log.error(f"stamp_metadata_on_image: failed to encode output — {e}")
        return b64_data