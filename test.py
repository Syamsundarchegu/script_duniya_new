import io
import textwrap
from PIL import Image, ImageDraw, ImageFont
import base64

def stamp_metadata_on_image(b64_data: str, metadata: dict) -> str:
    """Expands the image canvas and writes standard storyboard metadata at the bottom."""
    img_bytes = base64.b64decode(b64_data)
    original_img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    w, h = original_img.size

    # Height of the new white panel for text
    panel_height = 250
    new_img = Image.new("RGB", (w, h + panel_height), "white")
    
    # Paste original image at the top
    new_img.paste(original_img, (0, 0))
    draw = ImageDraw.Draw(new_img)

    # Attempt to load a readable font (OS fallbacks), otherwise use default
    try:
        font_title = ImageFont.truetype("arialbd.ttf", 26) # Windows Bold
        font_body = ImageFont.truetype("arial.ttf", 22)    # Windows Reg
    except IOError:
        try:
            font_title = ImageFont.truetype("DejaVuSans-Bold.ttf", 26) # Linux Bold
            font_body = ImageFont.truetype("DejaVuSans.ttf", 22)       # Linux Reg
        except IOError:
            font_title = ImageFont.load_default()
            font_body = ImageFont.load_default()

    text_x = 24
    text_y = h + 24

    # --- Line 1: Scene & Location Info ---
    scene_str = f"SCENE: {metadata.get('scene', 'N/A')}  |  FRAME: {metadata.get('frame_id', 'N/A')}"
    loc_str = f"{str(metadata.get('location', '')).upper()} - {str(metadata.get('time_of_day', '')).upper()}"
    draw.text((text_x, text_y), f"{scene_str}    |    {loc_str}", fill="black", font=font_title)

    # --- Line 2: Camera & Composition ---
    text_y += 40
    cam_str = f"SHOT TYPE: {str(metadata.get('shot_type', '')).title()}  |  COMPOSITION/MOVE: {str(metadata.get('composition', '')).title()}"
    draw.text((text_x, text_y), cam_str, fill="#333333", font=font_title)

    # --- Line 3: Action Description (Wrapped) ---
    text_y += 45
    action_raw = f"ACTION: {metadata.get('action', '')}"
    
    # Wrap text so it doesn't run off the image (approx 95 chars for a 1024px width image)
    wrapped_action = textwrap.wrap(action_raw, width=95)
    for line in wrapped_action:
        draw.text((text_x, text_y), line, fill="black", font=font_body)
        text_y += 30

    # Convert the modified image back to base64
    buffered = io.BytesIO()
    new_img.save(buffered, format="PNG")
    return base64.b64encode(buffered.getvalue()).decode('utf-8')