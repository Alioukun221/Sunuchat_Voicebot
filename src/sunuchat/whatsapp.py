"""Appels à l'API WhatsApp Cloud de Meta : envoyer texte et vocaux, télécharger les médias."""

import hashlib
import hmac
import logging

import httpx

from sunuchat.config import get_settings

log = logging.getLogger(__name__)

# WhatsApp limite un message texte à 4096 caractères.
MAX_TEXT = 4000


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {get_settings().whatsapp_access_token}"}


def _messages_url() -> str:
    s = get_settings()
    return f"{s.graph_url}/{s.whatsapp_phone_number_id}/messages"


def verify_signature(body: bytes, signature_header: str | None) -> bool:
    """Vérifie l'en-tête X-Hub-Signature-256 envoyé par Meta.

    Sans App Secret dans le .env, la vérification est désactivée (pratique en local).
    """
    secret = get_settings().whatsapp_app_secret
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.removeprefix("sha256="))


def parse_messages(payload: dict) -> list[dict]:
    """Extrait les messages entrants d'un webhook Meta (ignore les statuts de livraison)."""
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {
                c.get("wa_id"): c.get("profile", {}).get("name", "")
                for c in value.get("contacts", [])
            }
            for msg in value.get("messages", []):
                messages.append({**msg, "profile_name": names.get(msg.get("from"), "")})
    return messages


async def _post(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(_messages_url(), headers=_headers(), json=payload)
    if r.status_code >= 400:
        log.error("Erreur WhatsApp %s : %s", r.status_code, r.text)
    r.raise_for_status()
    return r.json()


async def send_text(to: str, text: str) -> None:
    for start in range(0, len(text), MAX_TEXT):
        await _post(
            {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "text",
                "text": {"body": text[start : start + MAX_TEXT]},
            }
        )


async def mark_read_and_typing(message_id: str) -> None:
    """Passe le message en coches bleues et affiche « en train d'écrire » (25 s max)."""
    try:
        await _post(
            {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
                "typing_indicator": {"type": "text"},
            }
        )
    except httpx.HTTPError:
        log.warning("Impossible d'afficher l'indicateur de saisie")


async def download_media(media_id: str) -> bytes:
    """Télécharge un média reçu (vocal, photo) : on récupère d'abord son URL, puis le
    fichier."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(f"{s.graph_url}/{media_id}", headers=_headers())
        r.raise_for_status()
        url = r.json()["url"]
        r = await client.get(url, headers=_headers())
        r.raise_for_status()
        return r.content


async def upload_media(
    data: bytes, mime_type: str = "audio/ogg", filename: str = "reponse.ogg"
) -> str:
    s = get_settings()
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            f"{s.graph_url}/{s.whatsapp_phone_number_id}/media",
            headers=_headers(),
            data={"messaging_product": "whatsapp", "type": mime_type},
            files={"file": (filename, data, mime_type)},
        )
    r.raise_for_status()
    return r.json()["id"]


async def send_audio(to: str, ogg_data: bytes) -> None:
    media_id = await upload_media(ogg_data)
    await _post(
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "audio",
            "audio": {"id": media_id},
        }
    )


async def send_video(to: str, mp4_data: bytes) -> None:
    media_id = await upload_media(mp4_data, "video/mp4", "rappel.mp4")
    await _post(
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "video",
            "video": {"id": media_id},
        }
    )


async def send_location(
    to: str, latitude: float, longitude: float, name: str = "", address: str = ""
) -> None:
    """Envoie une épingle sur la carte (ex. le poste de santé le plus proche)."""
    location = {"latitude": latitude, "longitude": longitude}
    if name:
        location["name"] = name
    if address:
        location["address"] = address
    await _post(
        {"messaging_product": "whatsapp", "to": to, "type": "location", "location": location}
    )


async def send_location_request(to: str, text: str) -> None:
    """Message avec un bouton « Envoyer la position » : l'utilisateur partage sa position en
    un clic, et elle revient dans le webhook comme un message de type location."""
    await _post(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": {
                "type": "location_request_message",
                "body": {"text": text},
                "action": {"name": "send_location"},
            },
        }
    )


# ---------- Boutons, menus, contacts, images, modèles ----------
# Limites WhatsApp : 3 boutons (titre ≤ 20 caractères), 10 lignes de menu (titre ≤ 24,
# description ≤ 72), texte du bouton de menu ≤ 20.


async def send_buttons(
    to: str, body: str, buttons: list[tuple[str, str]], image_id: str | None = None
) -> None:
    """Message avec jusqu'à 3 boutons de réponse [(id, titre)], et une image en en-tête."""
    interactive: dict = {
        "type": "button",
        "body": {"text": body},
        "action": {
            "buttons": [
                {"type": "reply", "reply": {"id": bid, "title": title[:20]}}
                for bid, title in buttons[:3]
            ]
        },
    }
    if image_id:
        interactive["header"] = {"type": "image", "image": {"id": image_id}}
    await _post(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }
    )


async def send_list(
    to: str, body: str, button: str, rows: list[tuple[str, str, str]], header: str = ""
) -> None:
    """Menu déroulant : un bouton qui ouvre jusqu'à 10 choix [(id, titre, description)]."""
    interactive: dict = {
        "type": "list",
        "body": {"text": body},
        "action": {
            "button": button[:20],
            "sections": [
                {
                    "title": (header or "Menu")[:24],
                    "rows": [
                        {"id": rid, "title": title[:24], "description": desc[:72]}
                        for rid, title, desc in rows[:10]
                    ],
                }
            ],
        },
    }
    if header:
        interactive["header"] = {"type": "text", "text": header[:60]}
    await _post(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "interactive",
            "interactive": interactive,
        }
    )


async def send_contact(to: str, name: str, phone: str) -> None:
    """Fiche contact : un appui sur « Appeler » lance l'appel (ex. SAMU 1515)."""
    await _post(
        {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "contacts",
            "contacts": [
                {
                    "name": {"formatted_name": name, "first_name": name},
                    "phones": [{"phone": phone, "type": "WORK"}],
                }
            ],
        }
    )


async def send_image(to: str, image_id: str, caption: str = "") -> None:
    image = {"id": image_id}
    if caption:
        image["caption"] = caption
    await _post({"messaging_product": "whatsapp", "to": to, "type": "image", "image": image})


async def send_template(
    to: str,
    name: str,
    language: str = "fr",
    params: list[str] | None = None,
    image_id: str | None = None,
) -> None:
    """Modèle de message approuvé par Meta : seul moyen d'écrire à quelqu'un qui n'a pas
    envoyé de message depuis plus de 24 h (rappels de vaccins, alertes de saison)."""
    components = []
    if image_id:
        components.append(
            {"type": "header", "parameters": [{"type": "image", "image": {"id": image_id}}]}
        )
    if params:
        components.append(
            {"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}
        )
    template: dict = {"name": name, "language": {"code": language}}
    if components:
        template["components"] = components
    await _post(
        {"messaging_product": "whatsapp", "to": to, "type": "template", "template": template}
    )


def interactive_reply_id(message: dict) -> str:
    """Identifiant du bouton ou de la ligne de menu choisis (message de type interactive)."""
    inter = message.get("interactive", {})
    reply = inter.get("button_reply") or inter.get("list_reply") or {}
    return reply.get("id", "")
