"""
Servicio con múltiples estrategias de remover fondo.
Cada método tiene pros y contras:
- rembg: mejor calidad, más lento (IA)
- watershed: buen balance, moderado
- threshold: muy rápido, solo fondos uniformes
"""

from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import numpy as np
from rembg import remove as rembg_remove
from scipy import ndimage as ndi
from skimage import exposure, segmentation, morphology, filters
import time
import cv2


def remove_bg_rembg(img: Image.Image, quality: str = "high") -> Image.Image:
    """
    Remover fondo usando rembg (IA con modelo ONNX).
    - Pros: Excelente calidad, maneja bordes complejos
    - Contras: Más lento (~2-3s por imagen)
    
    Args:
        img: Imagen PIL
        quality: "high" para mejor calidad (más lento), "normal" para balance
    """
    # Parámetros para mejor calidad
    alpha_matting = quality == "high"
    post_process_mask = quality == "high"
    
    try:
        # Usar rembg con parámetros mejorados
        img_no_bg = rembg_remove(
            img,
            alpha_matting=alpha_matting,
            alpha_matting_foreground_threshold=270 if quality == "high" else 240,
            alpha_matting_background_threshold=20,
            post_process_mask=post_process_mask
        )
    except:
        # Fallback si los parámetros no funcionan
        img_no_bg = rembg_remove(img)
    
    if img_no_bg.mode != 'RGBA':
        img_no_bg = img_no_bg.convert('RGBA')
    
    # Mejorar nitidez de bordes si es high quality
    if quality == "high":
        # Mejorar nitidez ligera
        enhancer = ImageEnhance.Sharpness(img_no_bg)
        img_no_bg = enhancer.enhance(1.2)
    
    # Compositar sobre fondo blanco
    background = Image.new('RGBA', img_no_bg.size, (255, 255, 255, 255))
    img_white_bg = Image.alpha_composite(background, img_no_bg)
    
    return img_white_bg


def remove_bg_watershed(img: Image.Image) -> Image.Image:
    """
    Remover fondo usando Watershed de scikit-image.
    - Pros: Rápido (~0.5s), bueno para objetos bien definidos
    - Contras: Menos preciso en bordes borrosos
    """
    if img.mode != 'RGB':
        img = img.convert('RGB')
    
    img_array = np.array(img)
    
    # Convertir a escala de grises
    gray = np.dot(img_array[..., :3], [0.299, 0.587, 0.114]).astype(np.uint8)
    
    # Threshold para obtener binary image
    threshold = filters.threshold_otsu(gray)
    binary = gray > threshold
    
    # Morphological operations
    binary = morphology.binary_opening(binary, morphology.disk(3))
    binary = morphology.binary_dilation(binary, morphology.disk(2))
    
    # Distance transform
    distance = ndi.distance_transform_edt(binary)
    
    # Encontrar markers
    coords = np.array(np.unravel_index(np.argsort(distance.ravel())[-100:], distance.shape)).T
    markers = ndi.label(binary)[0]
    
    # Watershed
    labels = segmentation.watershed(-distance, markers=markers, mask=binary)
    
    # Crear máscara: el fondo es label 0, el objeto es el resto
    mask = (labels > 0).astype(np.uint8) * 255
    
    # Aplicar máscara con smoothing
    mask_img = Image.fromarray(mask)
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius=2))
    
    result = img.convert('RGBA')
    result.putalpha(mask_img)
    
    return result


def _get_background_color(img_array: np.ndarray, margin: int = 50) -> tuple:
    """
    Detectar el color dominante del fondo muestreando las esquinas.
    Asume que las esquinas son principalmente fondo.
    
    Args:
        img_array: Array numpy de la imagen (H, W, 3)
        margin: Píxeles desde las esquinas a muestrear
    
    Returns:
        Color RGB promedio del fondo (r, g, b)
    """
    h, w, _ = img_array.shape
    
    # Muestrear esquinas (arriba-izq, arriba-der, abajo-izq, abajo-der)
    corners = [
        img_array[0:margin, 0:margin],           # Arriba-izquierda
        img_array[0:margin, w-margin:w],         # Arriba-derecha
        img_array[h-margin:h, 0:margin],         # Abajo-izquierda
        img_array[h-margin:h, w-margin:w],       # Abajo-derecha
    ]
    
    # Combinar todas las esquinas
    corner_pixels = np.vstack(corners).reshape(-1, 3)
    
    # Color promedio
    bg_color = np.mean(corner_pixels, axis=0).astype(np.uint8)
    return tuple(bg_color)


def _calculate_adaptive_threshold(bg_color: tuple) -> int:
    """
    Calcular threshold ADAPTATIVO basado en el color real del fondo.
    
    Args:
        bg_color: Color RGB del fondo (r, g, b)
    
    Returns:
        Valor de threshold (0-255)
    """
    # Convertir color RGB a escala de grises
    bg_gray = int(np.dot(bg_color, [0.299, 0.587, 0.114]))
    
    # Ajustar threshold según el brillo del fondo
    # Idea: el threshold debe estar entre el fondo y el objeto esperado
    if bg_gray > 200:  # Fondo muy claro (casi blanco)
        threshold = bg_gray - 50  # Bastante agresivo
    elif bg_gray > 150:  # Fondo claro (gris claro)
        threshold = bg_gray - 40
    elif bg_gray > 100:  # Fondo medio (gris)
        threshold = bg_gray - 30
    else:  # Fondo oscuro (gris oscuro/negro)
        threshold = bg_gray - 20
    
    # Asegurar rango válido (0-255)
    threshold = max(0, min(255, threshold))
    return threshold


def remove_bg_threshold_adaptive(img: Image.Image) -> Image.Image:
    """
    Remover fondo usando THRESHOLD ADAPTATIVO al color real del fondo.
    - Pros: Funciona con CUALQUIER color de fondo, súper rápido (<100ms)
    - Contras: Asume fondo uniforme en las esquinas
    
    Args:
        img: Imagen PIL
    """
    if img.mode != 'RGB':
        img = img.convert('RGB')
    
    img_array = np.array(img, dtype=np.uint8)
    
    # PASO 1: Detectar color del fondo (muestreando esquinas)
    bg_color = _get_background_color(img_array, margin=50)
    
    # PASO 2: Calcular threshold adaptativo basado en ese color
    adaptive_threshold = _calculate_adaptive_threshold(bg_color)
    
    # PASO 3: Aplicar threshold (igual que threshold_simple pero con valor dinámico)
    gray = np.dot(img_array[..., :3], [0.299, 0.587, 0.114]).astype(np.uint8)
    
    # Crear máscara: píxeles por debajo del threshold son objeto
    mask = (gray < adaptive_threshold).astype(np.uint8) * 255
    
    # Aplicar morphological closing para suavizar
    mask = morphology.binary_closing(mask.astype(bool), morphology.disk(5))
    mask = (mask.astype(np.uint8) * 255)
    
    # Suavizar bordes
    mask_img = Image.fromarray(mask)
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius=3))
    
    result = img.convert('RGBA')
    result.putalpha(mask_img)
    
    return result


def remove_bg_threshold_simple(img: Image.Image, threshold: int = 200) -> Image.Image:
    """
    Remover fondo usando threshold simple (color uniforme).
    - Pros: Súper rápido (<100ms), perfecto para fondos sólidos
    - Contras: Solo para fondos uniformes claros
    
    Args:
        img: Imagen PIL
        threshold: Valor de threshold (0-255)
    """
    if img.mode != 'RGB':
        img = img.convert('RGB')
    
    img_array = np.array(img, dtype=np.uint8)
    gray = np.dot(img_array[..., :3], [0.299, 0.587, 0.114]).astype(np.uint8)
    
    # Crear máscara: píxeles oscuros son objeto
    mask = (gray < threshold).astype(np.uint8) * 255
    
    # Aplicar morphological closing para suavizar
    mask = morphology.binary_closing(mask.astype(bool), morphology.disk(5))
    mask = (mask.astype(np.uint8) * 255)
    
    # Suavizar bordes
    mask_img = Image.fromarray(mask)
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius=3))
    
    result = img.convert('RGBA')
    result.putalpha(mask_img)
    
    return result


def remove_bg_laplacian_edge(img: Image.Image) -> Image.Image:
    """
    Remover fondo usando Laplacian edge detection.
    - Pros: Rápido (~0.3s), bueno para objetos con bordes definidos
    - Contras: No funciona bien con bordes borrosos
    """
    if img.mode != 'RGB':
        img = img.convert('RGB')
    
    img_array = np.array(img)
    gray = np.dot(img_array[..., :3], [0.299, 0.587, 0.114]).astype(np.uint8)
    
    # Aplicar Laplacian
    laplacian = filters.laplace(gray)
    
    # Crear máscara a partir de los bordes
    edges = np.abs(laplacian)
    edges = (edges > np.percentile(edges, 40)).astype(np.uint8) * 255
    
    # Dilate para conectar regiones
    edges = morphology.binary_dilation(edges.astype(bool), morphology.disk(3))
    edges = (edges.astype(np.uint8) * 255)
    
    # Rellenar huecos (fill interior)
    edges = ndi.binary_fill_holes(edges.astype(bool))
    mask = (edges.astype(np.uint8) * 255)
    
    # Suavizar bordes
    mask_img = Image.fromarray(mask)
    mask_img = mask_img.filter(ImageFilter.GaussianBlur(radius=2))
    
    result = img.convert('RGBA')
    result.putalpha(mask_img)
    
    return result


def remove_watermark(img: Image.Image, method: str = "inpaint") -> Image.Image:
    """
    Remover watermarks/marcas de agua de la imagen.
    
    Métodos:
    - "inpaint": Usa inpainting content-aware para rellenar áreas
    - "contrast": Detecta y elimina áreas de bajo contraste
    
    Args:
        img: Imagen PIL
        method: "inpaint" o "contrast"
    
    Returns:
        Imagen sin watermark
    """
    if img.mode != 'RGB':
        img_rgb = img.convert('RGB')
    else:
        img_rgb = img
    
    img_array = np.array(img_rgb, dtype=np.uint8)
    
    if method == "inpaint":
        return _remove_watermark_inpaint(img_array, img_rgb)
    elif method == "contrast":
        return _remove_watermark_contrast(img_array, img_rgb)
    else:
        return img_rgb


def _remove_watermark_inpaint(img_array: np.ndarray, img_pil: Image.Image) -> Image.Image:
    """
    Remover watermark usando inpainting (Telea algorithm).
    Detecta automáticamente áreas de watermark y las rellena.
    """
    img_cv = cv2.cvtColor(img_array, cv2.COLOR_RGB2BGR)
    
    # Convertir a escala de grises para detectar watermark
    gray = cv2.cvtColor(img_cv, cv2.COLOR_BGR2GRAY)
    
    # Detectar áreas de bajo contraste/brillo anómalo (típicas de watermarks)
    # Usar threshold adaptativo
    blur = cv2.GaussianBlur(gray, (21, 21), 0)
    diff = cv2.absdiff(gray, blur)
    
    # Threshold para encontrar áreas anómalas
    _, mask = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)
    
    # Dilate para expandir la máscara de watermark
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    mask = cv2.dilate(mask, kernel, iterations=2)
    
    # Aplicar inpainting solo si hay áreas detectadas
    if mask.sum() > 0:
        # Usar el método Telea para inpainting
        inpainted = cv2.inpaint(img_cv, mask, 3, cv2.INPAINT_TELEA)
        result_rgb = cv2.cvtColor(inpainted, cv2.COLOR_BGR2RGB)
        return Image.fromarray(result_rgb)
    
    return img_pil


def _remove_watermark_contrast(img_array: np.ndarray, img_pil: Image.Image) -> Image.Image:
    """
    Remover watermark detectando áreas de bajo contraste.
    """
    gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
    
    # Calcular contraste local usando Laplacian
    laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
    
    # Si el contraste es muy bajo, la imagen probablemente tiene watermark
    if laplacian_var < 100:
        # Mejorar contraste
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(gray)
        result = cv2.cvtColor(enhanced, cv2.COLOR_GRAY2RGB)
        return Image.fromarray(result)
    
    # Si contraste es normal, aplicar denoising ligero
    denoised = cv2.fastNlMeansDenoisingColored(img_array, None, h=10, hForColorComponents=10, templateWindowSize=7, searchWindowSize=21)
    return Image.fromarray(denoised)


def _is_background_white(img_array: np.ndarray, margin: int = 50, threshold: int = 200) -> bool:
    """
    Detectar si el fondo es BLANCO (necesario para usar threshold combinado).
    
    Args:
        img_array: Array numpy de la imagen (H, W, 3)
        margin: Píxeles desde las esquinas a muestrear
        threshold: Valor mínimo de brillo para considerar "blanco" (0-255)
    
    Returns:
        True si fondo es principalmente blanco, False si es otro color
    """
    h, w, _ = img_array.shape
    
    # Muestrear esquinas
    corners = [
        img_array[0:margin, 0:margin],
        img_array[0:margin, w-margin:w],
        img_array[h-margin:h, 0:margin],
        img_array[h-margin:h, w-margin:w],
    ]
    
    corner_pixels = np.vstack(corners).reshape(-1, 3)
    
    # Convertir a escala de grises
    gray_values = np.dot(corner_pixels, [0.299, 0.587, 0.114])
    
    # Si más del 70% de los píxeles de esquina son "blancos", es fondo blanco
    white_count = np.sum(gray_values > threshold)
    white_percentage = (white_count / len(gray_values)) * 100
    
    return white_percentage > 70


def remove_bg_combined(img: Image.Image, quality: str = "high") -> Image.Image:
    """
    Remover fondo de forma INTELIGENTE según el color del fondo.
    
    Estrategia ADAPTATIVA:
    - Si fondo es BLANCO: Usar Threshold adaptativo + Rembg (mejor limpieza)
    - Si fondo es OTRO COLOR: Usar solo Rembg (más inteligente para fondos variados)
    
    Beneficios:
    - Funciona con CUALQUIER color de fondo
    - Fondos blancos: Limpieza máxima (elimina watermarks)
    - Fondos otros colores: Calidad máxima (rembg es superior)
    
    Args:
        img: Imagen PIL
        quality: "high" para mejor calidad, "normal" para balance
    
    Returns:
        Imagen RGBA con fondo removido
    """
    if img.mode != 'RGB':
        img = img.convert('RGB')
    
    img_array = np.array(img, dtype=np.uint8)
    
    # PASO 1: Detectar si el fondo es blanco
    is_white_bg = _is_background_white(img_array, margin=50, threshold=200)
    
    # PASO 2: Elegir estrategia según el fondo
    if is_white_bg:
        # ===== FONDO BLANCO: Usar combinación (Threshold + Rembg) =====
        # Esto elimina watermarks y da máxima limpieza
        
        # Obtener máscara del Threshold ADAPTATIVO
        threshold_result = remove_bg_threshold_adaptive(img)
        threshold_mask = threshold_result.split()[3]
        
        # Procesar con Rembg
        alpha_matting = quality == "high"
        post_process_mask = quality == "high"
        
        try:
            img_no_bg = rembg_remove(
                img,
                alpha_matting=alpha_matting,
                alpha_matting_foreground_threshold=270 if quality == "high" else 240,
                alpha_matting_background_threshold=20,
                post_process_mask=post_process_mask
            )
        except:
            img_no_bg = rembg_remove(img)
        
        if img_no_bg.mode != 'RGBA':
            img_no_bg = img_no_bg.convert('RGBA')
        
        rembg_mask = img_no_bg.split()[3]
        
        # Combinar máscaras: 95% Threshold (agresivo) + 5% Rembg (suaviza)
        threshold_array = np.array(threshold_mask, dtype=np.float32)
        rembg_array = np.array(rembg_mask, dtype=np.float32)
        combined_mask = (threshold_array * 0.95 + rembg_array * 0.05).astype(np.uint8)
        combined_mask_img = Image.fromarray(combined_mask, mode='L')
        
        result = img.convert('RGBA')
        result.putalpha(combined_mask_img)
        
    else:
        # ===== FONDO NO BLANCO: Usar solo Rembg (más inteligente) =====
        # Rembg es mejor para fondos de colores variados
        
        alpha_matting = quality == "high"
        post_process_mask = quality == "high"
        
        try:
            result = rembg_remove(
                img,
                alpha_matting=alpha_matting,
                alpha_matting_foreground_threshold=270 if quality == "high" else 240,
                alpha_matting_background_threshold=20,
                post_process_mask=post_process_mask
            )
        except:
            result = rembg_remove(img)
        
        if result.mode != 'RGBA':
            result = result.convert('RGBA')
    
    # Mejorar nitidez LIGERA si es high quality (evita artefactos)
    if quality == "high":
        enhancer = ImageEnhance.Sharpness(result)
        result = enhancer.enhance(1.05)  # Sharpening muy ligero para no crear artefactos
    
    # Compositar sobre fondo blanco
    background = Image.new('RGBA', result.size, (255, 255, 255, 255))
    img_white_bg = Image.alpha_composite(background, result)
    
    return img_white_bg


def apply_background_removal(img: Image.Image, method: str = "rembg", quality: str = "high") -> Image.Image:
    """
    Aplicar remover de fondo con el método especificado.
    
    Args:
        img: Imagen PIL
        method: "rembg", "watershed", "threshold_simple", "laplacian_edge", "combined"
        quality: "high" para mejor calidad, "normal" para balance
    
    Returns:
        Imagen RGBA con fondo removido
    """
    method = method.lower().strip()
    
    if method == "rembg":
        return remove_bg_rembg(img, quality=quality)
    elif method == "combined":
        return remove_bg_combined(img, quality=quality)
    elif method == "watershed":
        return remove_bg_watershed(img)
    elif method == "threshold_simple":
        return remove_bg_threshold_simple(img)
    elif method == "laplacian_edge":
        return remove_bg_laplacian_edge(img)
    else:
        raise ValueError(f"Método desconocido: {method}. Opciones: rembg, combined, watershed, threshold_simple, laplacian_edge")
