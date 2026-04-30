from PIL import Image, ImageOps
import numpy as np
from app.services.background_removal_service import apply_background_removal

def process_image_pipeline(img: Image.Image) -> Image.Image:
    """
    Pipeline optimizado de procesamiento de imágenes.
    Ahora usa el método COMBINADO inteligente que:
    - Si fondo es blanco: Threshold + Rembg (máxima limpieza, elimina watermarks)
    - Si fondo es otro color: Solo Rembg (mejor para fondos variados)
    
    Luego redimensiona a 2000x2000 cuadrado.
    """
    # 1. Remove background usando método COMBINADO INTELIGENTE
    # Detecta automáticamente el color del fondo y elige la mejor estrategia
    img_no_bg = apply_background_removal(img, method="combined", quality="high")
    
    # 2. Asegurar que es RGBA
    if img_no_bg.mode != 'RGBA':
        img_no_bg = img_no_bg.convert('RGBA')
    
    # 3. Reemplazar fondo transparente con blanco
    background = Image.new('RGBA', img_no_bg.size, (255, 255, 255, 255))
    img_white_bg = Image.alpha_composite(background, img_no_bg)
    
    # 4. Hacer cuadrado con padding
    max_side = max(img_white_bg.size)
    square_img = Image.new('RGBA', (max_side, max_side), (255, 255, 255, 255))
    offset = ((max_side - img_white_bg.width) // 2, (max_side - img_white_bg.height) // 2)
    square_img.paste(img_white_bg, offset)
    
    # 5. Redimensionar a 2000x2000
    resized_img = square_img.resize((2000, 2000), Image.Resampling.LANCZOS)
    
    # 6. Convertir a RGB para JPG export
    final_img = resized_img.convert('RGB')
    return final_img
