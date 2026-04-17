from app.domain.image_pipeline import process_image_pipeline
from app.utils.download import download_image
from io import BytesIO

async def optimize_image_from_url(url: str) -> bytes:
    img = await download_image(url)
    img = process_image_pipeline(img)
    buf = BytesIO()
    try:
        img.save(buf, format="JPEG", quality=95)
        return buf.getvalue()
    finally:
        buf.close()
