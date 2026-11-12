"""
Server-side validation and normalization of uploaded receipt photos.

- Only JPEG / PNG / WEBP are decoded (HEIC is passed through to Gemini, which
  accepts it natively, unless pillow-heif is installed).
- Decompression bombs are refused (MAX_IMAGE_PIXELS).
- EXIF orientation is applied, then the image is re-encoded as JPEG, which
  also strips EXIF/GPS metadata.
- Receipts are tall: cap the *width* at 1600px and the height at 4000px
  rather than the longest side, so long receipts stay legible.
"""
import base64
from io import BytesIO
from typing import Tuple

from PIL import Image, ImageOps, UnidentifiedImageError

Image.MAX_IMAGE_PIXELS = 40_000_000

MAX_UPLOAD_BYTES = 4 * 1024 * 1024
MAX_WIDTH = 1600
MAX_HEIGHT = 4000
JPEG_QUALITY = 85
_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP", "MPO"}
_HEIF_BRANDS = {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1", b"heim", b"heis"}

try:  # optional
    import pillow_heif  # type: ignore

    pillow_heif.register_heif_opener()
    _ALLOWED_FORMATS.add("HEIF")
    HEIF_DECODE = True
except Exception:
    HEIF_DECODE = False


class ImageRejected(ValueError):
    pass


def _is_heif(raw: bytes) -> bool:
    return len(raw) >= 12 and raw[4:8] == b"ftyp" and raw[8:12] in _HEIF_BRANDS


def prepare_receipt_image(raw: bytes) -> Tuple[str, str]:
    """Return (base64_data, mime_type) ready for a vision model, or raise ImageRejected."""
    if not raw:
        raise ImageRejected("The uploaded file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ImageRejected("Image is too large (max 4 MB). Try a smaller photo.")

    if _is_heif(raw) and not HEIF_DECODE:
        # Gemini accepts HEIC directly; we can't resize it, but it's under the size cap.
        return base64.b64encode(raw).decode("ascii"), "image/heic"

    try:
        with Image.open(BytesIO(raw)) as probe:
            fmt = probe.format
            probe.verify()
    except Image.DecompressionBombError as exc:
        raise ImageRejected("Image resolution is too large.") from exc
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageRejected("That file isn't a readable image. Use a JPG or PNG photo.") from exc
    if fmt not in _ALLOWED_FORMATS:
        raise ImageRejected("Unsupported image type. Use JPG, PNG or WEBP.")

    try:
        # verify() leaves the image unusable; reopen for real decoding.
        with Image.open(BytesIO(raw)) as img:
            img = ImageOps.exif_transpose(img)
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            scale = min(1.0, MAX_WIDTH / img.width, MAX_HEIGHT / img.height)
            if scale < 1.0:
                img = img.resize(
                    (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                    Image.LANCZOS,
                )
            out = BytesIO()
            img.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    except Image.DecompressionBombError as exc:
        raise ImageRejected("Image resolution is too large.") from exc
    except OSError as exc:
        raise ImageRejected("Could not process that image. Try another photo.") from exc

    return base64.b64encode(out.getvalue()).decode("ascii"), "image/jpeg"
