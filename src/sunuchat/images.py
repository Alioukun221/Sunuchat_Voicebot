"""Visuels envoyés sur WhatsApp (accueil, alerte d'hivernage).

Il suffit de déposer les images dans assets/images/ avec ces noms (jpg ou png) :
  accueil (dessin du bouton micro), hivernage
Une image absente est simplement sautée. Chaque image est envoyée une fois à Meta, puis
son identifiant est réutilisé (gardé 25 jours dans data/images.json : Meta les garde 30).
"""

import json
import logging
import time
from pathlib import Path

from sunuchat.config import ROOT, get_settings

log = logging.getLogger(__name__)

IMAGES_DIR = ROOT / "assets" / "images"
MAX_AGE_S = 25 * 24 * 3600
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def find(name: str, folder: Path | None = None) -> Path | None:
    folder = folder or IMAGES_DIR
    for ext in MIME:
        path = folder / f"{name}{ext}"
        if path.exists():
            return path
    return None


def _cache_path() -> Path:
    return get_settings().data_dir / "images.json"


def _load_cache() -> dict:
    try:
        return json.loads(_cache_path().read_text())
    except (OSError, ValueError):
        return {}


async def media_id(name: str) -> str | None:
    """Identifiant WhatsApp de l'image `name`, ou None si elle n'existe pas ou n'a pas pu
    être envoyée (le message part alors sans image)."""
    from sunuchat import whatsapp

    path = find(name)
    if path is None:
        return None
    cache = _load_cache()
    key = f"{path.name}:{int(path.stat().st_mtime)}"
    entry = cache.get(name)
    if entry and entry["key"] == key and time.time() - entry["at"] < MAX_AGE_S:
        return entry["id"]
    try:
        mid = await whatsapp.upload_media(path.read_bytes(), MIME[path.suffix.lower()], path.name)
    except Exception:
        log.exception("Image %s non envoyée à WhatsApp", path.name)
        return None
    cache[name] = {"key": key, "id": mid, "at": time.time()}
    _cache_path().write_text(json.dumps(cache, indent=2))
    return mid


async def send(to: str, name: str, caption: str = "") -> None:
    """Envoie l'image `name` si elle existe ; sinon ne fait rien."""
    from sunuchat import whatsapp

    mid = await media_id(name)
    if mid:
        try:
            await whatsapp.send_image(to, mid, caption)
        except Exception:
            log.exception("Image %s non envoyée", name)
