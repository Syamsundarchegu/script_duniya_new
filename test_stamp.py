# import base64
# import os
# from PIL import Image
# import io

# # ── Create a dummy test image (white 800x600) ──────────────────────────────
# def make_dummy_b64_image():
#     img = Image.new("RGB", (800, 600), color=(200, 200, 200))
#     buf = io.BytesIO()
#     img.save(buf, format="PNG")
#     buf.seek(0)
#     return base64.b64encode(buf.read()).decode("utf-8")

# # ── Dummy metadata ─────────────────────────────────────────────────────────
# test_metadata = {
#     "scene":       "01",
#     "frame_id":    "S01F001",
#     "location":    "Office Interior",
#     "time_of_day": "Day",
#     "shot_type":   "WIDE SHOT",
#     "action":      "Character walks into the room and looks around carefully.",
#     "composition": "Camera slowly pans left to right following the character.",
# }

# # ── Run stamp ──────────────────────────────────────────────────────────────
# from stamp import stamp_metadata_on_image

# print("Running stamp test...")
# result_b64 = stamp_metadata_on_image(make_dummy_b64_image(), test_metadata)

# # ── Save output so you can visually verify ────────────────────────────────
# output_path = "test_output.jpg"
# with open(output_path, "wb") as f:
#     f.write(base64.b64decode(result_b64))

# print(f"✅ Done! Open '{output_path}' to visually verify the banner.")
# print(f"   Output size: {os.path.getsize(output_path) / 1024:.1f} KB")




import math
from PIL import Image, ImageDraw

def _draw_movement_arrow(img: Image.Image, notation: str) -> Image.Image:
    """Draws a clean, programmatic arrow with a solid arrowhead based on movement notation."""
    draw = ImageDraw.Draw(img)
    width, height = img.size
    
    # Define arrow dimensions dynamically based on image size
    arrow_len = int(width * 0.15)
    stem_width = max(3, int(height * 0.015))
    head_size = max(15, int(height * 0.05))
    
    m = notation.lower()
    
    def draw_real_arrow(start, end):
        # 1. Calculate the angle of the line
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        angle = math.atan2(dy, dx)
        
        # 2. Calculate where the stem should stop so it doesn't poke through the tip
        stem_end = (
            end[0] - (head_size * 0.5) * math.cos(angle),
            end[1] - (head_size * 0.5) * math.sin(angle)
        )
        
        # 3. Draw the main stem (black outline, then white interior)
        draw.line([start, stem_end], fill="black", width=stem_width + 4)
        draw.line([start, stem_end], fill="white", width=stem_width)
        
        # 4. Calculate the three points of the arrowhead (triangle)
        arrow_angle = math.pi / 6  # 30-degree angle for the arrow wings
        
        tip = end
        left = (
            end[0] - head_size * math.cos(angle - arrow_angle), 
            end[1] - head_size * math.sin(angle - arrow_angle)
        )
        right = (
            end[0] - head_size * math.cos(angle + arrow_angle), 
            end[1] - head_size * math.sin(angle + arrow_angle)
        )
        
        # 5. Draw the arrowhead (white fill)
        draw.polygon([tip, left, right], fill="white")
        
        # 6. Draw the thick black outline around the arrowhead
        # Using a line loop ensures a clean border on all PIL versions
        draw.line([tip, left, right, tip], fill="black", width=4, joint="curve")

    # Calculate coordinates based on direction
    if any(x in m for x in ["→", "right", "pan right", "tracking right"]):
        start = (width * 0.1, height * 0.85)
        end = (width * 0.1 + arrow_len, height * 0.85)
        draw_real_arrow(start, end)
        
    elif any(x in m for x in ["←", "left", "pan left", "tracking left"]):
        start = (width * 0.9, height * 0.85)
        end = (width * 0.9 - arrow_len, height * 0.85)
        draw_real_arrow(start, end)
        
    elif any(x in m for x in ["↑", "tilt up", "crane up", "upward"]):
        start = (width * 0.1, height * 0.9)
        end = (width * 0.1, height * 0.9 - arrow_len)
        draw_real_arrow(start, end)
        
    elif any(x in m for x in ["↓", "tilt down", "crane down", "downward"]):
        start = (width * 0.1, height * 0.1)
        end = (width * 0.1, height * 0.1 + arrow_len)
        draw_real_arrow(start, end)

    return img



# --- TEST HARNESS ---
def run_tests():
    print("Generating test images...")
    
    # Create a 1536x1024 dark gray image to simulate your standard frame size
    base_img = Image.new('RGB', (1536, 1024), color='#333333')

    # Test 1: Pan Right
    img_right = base_img.copy()
    img_right = _draw_movement_arrow(img_right, "pan right")
    img_right.save("test_arrow_right.jpg")
    print("✅ Created test_arrow_right.jpg")

    # Test 2: Pan Left
    img_left = base_img.copy()
    img_left = _draw_movement_arrow(img_left, "pan left")
    img_left.save("test_arrow_left.jpg")
    print("✅ Created test_arrow_left.jpg")

    # Test 3: Tilt Up
    img_up = base_img.copy()
    img_up = _draw_movement_arrow(img_up, "tilt up")
    img_up.save("test_arrow_up.jpg")
    print("✅ Created test_arrow_up.jpg")

    # Test 4: Tilt Down
    img_down = base_img.copy()
    img_down = _draw_movement_arrow(img_down, "tilt down")
    img_down.save("test_arrow_down.jpg")
    print("✅ Created test_arrow_down.jpg")

    print("\nDone! Open the folder to check the images.")

if __name__ == "__main__":
    run_tests()