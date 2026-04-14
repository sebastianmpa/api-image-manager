from PIL import Image, ImageOps
import numpy as np
from rembg import remove

def process_image_pipeline(img: Image.Image) -> Image.Image:
    # 1. Remove background
    img_no_bg = remove(img)
    # 2. Convert to RGBA if not already
    if img_no_bg.mode != 'RGBA':
        img_no_bg = img_no_bg.convert('RGBA')
    # 3. Replace transparent background with white
    background = Image.new('RGBA', img_no_bg.size, (255, 255, 255, 255))
    img_white_bg = Image.alpha_composite(background, img_no_bg)
    # 4. Make square with padding
    max_side = max(img_white_bg.size)
    square_img = Image.new('RGBA', (max_side, max_side), (255, 255, 255, 255))
    offset = ((max_side - img_white_bg.width) // 2, (max_side - img_white_bg.height) // 2)
    square_img.paste(img_white_bg, offset)
    # 5. Resize to 2000x2000
    resized_img = square_img.resize((2000, 2000), Image.LANCZOS)
    # 6. Convert to RGB for JPG export
    final_img = resized_img.convert('RGB')
    return final_img
