"""
Legacy image-based OCR using the Gemini Vision API directly.

This module is only active when LEGACY_IMAGE_OCR_ENABLED=true.
For the primary receipt extraction pipeline, see receipt_pipeline.py.
"""

import base64
import json
import os
import random
from typing import List, Optional

import redis
import requests
from dotenv import load_dotenv

from .receipt_rules import build_image_receipt_prompt

load_dotenv()


# ---------------------------------------------------------------------------
# Redis client for API key rotation
# ---------------------------------------------------------------------------

def _get_key_rotation_redis() -> Optional[redis.Redis]:
    """Return a Redis client used solely for Gemini API key rotation.

    Supports both REDIS_URL (cloud/Vercel) and REDIS_HOST/PORT (self-hosted).
    """
    redis_url = os.getenv("REDIS_URL", "").strip()
    try:
        if redis_url:
            client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        else:
            client = redis.Redis(
                host=os.getenv("REDIS_HOST", "localhost"),
                port=int(os.getenv("REDIS_PORT", 6379)),
                password=os.getenv("REDIS_PASSWORD", None),
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
            )
        client.ping()
        return client
    except Exception as exc:
        print(f"Redis connection failed for OCR key rotation: {exc}")
        return None


_key_rotation_redis = _get_key_rotation_redis()


# ---------------------------------------------------------------------------
# Gemini API key management
# ---------------------------------------------------------------------------

def _collect_gemini_keys() -> List[str]:
    candidates = [
        os.getenv("GEMINI_API_KEYS", ""),
        os.getenv("GEMINI_API_KEY", ""),
        os.getenv("gemini-api-key", ""),
    ]
    keys: List[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        keys.extend([k.strip() for k in candidate.split(",") if k.strip()])
    return keys


def _initialize_api_keys() -> None:
    if not _key_rotation_redis:
        return
    try:
        keys = _collect_gemini_keys()
        if keys and not _key_rotation_redis.exists("ocr:gemini_keys"):
            _key_rotation_redis.rpush("ocr:gemini_keys", *keys)
            _key_rotation_redis.set("ocr:gemini_key_index", 0)
            print(f"Initialized {len(keys)} Gemini API key(s) in Redis")
        if not _key_rotation_redis.exists("ocr:gemini_key_index"):
            _key_rotation_redis.set("ocr:gemini_key_index", 0)
    except Exception as exc:
        print(f"Gemini key initialization failed: {exc}")


def _get_next_api_key() -> Optional[str]:
    if _key_rotation_redis:
        try:
            index = int(_key_rotation_redis.get("ocr:gemini_key_index") or 0)
            keys = _key_rotation_redis.lrange("ocr:gemini_keys", 0, -1)
            if keys:
                selected = keys[index % len(keys)]
                _key_rotation_redis.set("ocr:gemini_key_index", (index + 1) % len(keys))
                return selected
        except Exception as exc:
            print(f"Redis API key rotation failed: {exc}")

    keys = _collect_gemini_keys()
    return random.choice(keys) if keys else None


_initialize_api_keys()


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

def _extract_json_text(model_text: str) -> str:
    cleaned = model_text.strip().strip("`").strip()
    if cleaned.startswith("json"):
        cleaned = cleaned[4:].strip()

    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned

    start = cleaned.find("{")
    if start == -1:
        return cleaned

    depth = 0
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return cleaned[start : index + 1]
    return cleaned


# ---------------------------------------------------------------------------
# OCR class
# ---------------------------------------------------------------------------

class OcrBillMaker:
    """Send a receipt image to Gemini Vision and return structured receipt data."""

    def __init__(self):
        self.api_key = _get_next_api_key()
        if not self.api_key:
            raise ValueError("No Gemini API key available")

        model = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.0-flash")
        # API key goes in a header — not the URL — to keep it out of server logs.
        self.url = (
            f"https://generativelanguage.googleapis.com/v1beta/models"
            f"/{model}:generateContent"
        )
        self.headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": self.api_key,
        }
        self.prompt = build_image_receipt_prompt()

    def get_text(self, file_input) -> dict:
        """
        Extract receipt data from a file path or file-like object.

        Returns a dict matching the receipt JSON schema.
        """
        if isinstance(file_input, str):
            with open(file_input, "rb") as fh:
                image_bytes = fh.read()
        else:
            file_input.seek(0)
            image_bytes = file_input.read()

        encoded = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "contents": [{
                "parts": [
                    {"text": self.prompt},
                    {"inline_data": {"mime_type": "image/jpeg", "data": encoded}},
                ]
            }]
        }

        response = requests.post(self.url, headers=self.headers, json=payload, timeout=30)
        response.raise_for_status()

        content = response.json()
        model_text = content["candidates"][0]["content"]["parts"][0]["text"]
        json_text = _extract_json_text(model_text)
        return json.loads(json_text)

