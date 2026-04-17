import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from PIL import Image
from io import BytesIO
from urllib.parse import urlparse

# Estrategias de headers a intentar en orden
def _header_strategies(url: str) -> list:
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    base = {
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Cache-Control": "no-cache",
    }

    return [
        # 1. Chrome desktop + Referer propio dominio
        {**base,
         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
         "Referer": f"{origin}/"},
        # 2. Chrome desktop sin Referer (algunos CDN bloquean si Referer no es exacto)
        {**base,
         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"},
        # 3. Firefox desktop sin Referer
        {**base,
         "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
         "Accept": "image/avif,image/webp,*/*"},
        # 4. Googlebot (algunos sitios permiten crawlers)
        {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
        # 5. Sin headers (último recurso)
        {},
    ]


def _build_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


async def download_image(url: str) -> Image.Image:
    session = _build_session()
    last_error = None

    for i, headers in enumerate(_header_strategies(url)):
        try:
            resp = session.get(url, headers=headers, timeout=20, verify=False)
            if resp.status_code == 403:
                last_error = f"403 Client Error: Forbidden for url: {url}"
                print(f"  ⚠ Estrategia {i + 1} devolvió 403, reintentando con headers distintos...")
                continue
            resp.raise_for_status()
            return Image.open(BytesIO(resp.content))
        except Exception as e:
            last_error = str(e)
            if "403" not in str(e):
                # Error que no es 403 (timeout, DNS, etc.) — no tiene sentido reintentar con otros headers
                raise

    raise Exception(last_error)

