from PIL import Image, ImageOps, ImageEnhance
import numpy as np

def preprocess_image_bytes(file_bytes: bytes, max_side: int = 1800) -> np.ndarray:
    """
    Returns a numpy array ready for EasyOCR.
    Light preprocessing: grayscale, autocontrast, sharpen, resize.
    """
    img = Image.open(io.BytesIO(file_bytes)).convert("RGB")

    # Rotate if EXIF says so
    img = ImageOps.exif_transpose(img)

    # Resize (keep ratio)
    w, h = img.size
    scale = min(1.0, max_side / float(max(w, h)))
    if scale < 1.0:
        img = img.resize((int(w * scale), int(h * scale)))

    # Grayscale + contrast
    gray = ImageOps.grayscale(img)
    gray = ImageOps.autocontrast(gray)

    # Slight contrast boost + sharpen
    gray = ImageEnhance.Contrast(gray).enhance(1.2)
    gray = gray.filter(ImageFilter.SHARPEN)

    return np.array(gray)

# Imports required by PIL filters
import io
from PIL import ImageFilter