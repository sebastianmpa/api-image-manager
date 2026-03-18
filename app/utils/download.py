import requests
from PIL import Image
from io import BytesIO

async def download_image(url: str) -> Image.Image:
    resp = requests.get(url, verify=False)
    resp.raise_for_status()
    return Image.open(BytesIO(resp.content))
