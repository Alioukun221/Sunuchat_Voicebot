"""Accueil et menu en boutons, pour que même les personnes qui ne lisent pas sachent quoi
demander : au premier message, une carte avec 3 boutons de langue ; après le choix, un
vocal de bienvenue dans la langue et un menu (paludisme, dengue, vaccins, poste de santé,
urgence). Les textes sont dans textes.py.
"""

import logging
import re

from sunuchat import campaigns, images, memory, speech, textes, whatsapp
from sunuchat.config import get_settings

log = logging.getLogger(__name__)

_GREETING = re.compile(
    r"^\W*(salam|salut|slt|bonjour|bonsoir|hello|hi|cc|coucou|asalaa?m?\w*|as+alam\w*"
    r"|no mbaɗ\w*|no mbad\w*|na nga def|nanga def|jam|jàmm|jamm)\b",
    re.IGNORECASE,
)
_COMMANDS = {
    "menu": "menu",
    "tànnal": "menu",
    "tannal": "menu",
    "suɓo": "menu",
    "stop": "stop",
    "arret": "stop",
    "arrêt": "stop",
    "alerte": "alerte",
    "alertes": "alerte",
}
_voice_cache: dict[str, bytes] = {}


def is_greeting(text: str) -> bool:
    """Simple salutation (rien à répondre de plus que l'accueil)."""
    text = (text or "").strip()
    return len(text) <= 40 and bool(_GREETING.match(text))


def command(text: str) -> str | None:
    return _COMMANDS.get((text or "").strip().strip(".!").lower())


def language_of(profile: dict) -> str:
    langue = profile.get("langue") or get_settings().default_language
    return langue if langue in textes.LANGUES else "wolof"


async def send_language_card(user: str) -> None:
    await whatsapp.send_buttons(
        user, textes.ACCUEIL_CARTE, textes.BOUTONS_LANGUE, await images.media_id("accueil")
    )


async def send_menu(user: str, langue: str) -> None:
    await whatsapp.send_list(
        user, textes.MENU_TEXTE[langue], textes.MENU_BOUTON[langue], textes.MENU[langue]
    )


async def say(user: str, text: str, langue: str) -> None:
    """Vocal en wolof ou pulaar (les textes fixes sont mis en cache), texte sinon."""
    if langue in speech.TTS_VOICES:
        try:
            ogg = _voice_cache.get(text) or await speech.text_to_voice_note(text, langue)
            _voice_cache[text] = ogg
            await whatsapp.send_audio(user, ogg)
            return
        except Exception:
            log.exception("Vocal non généré : envoi en texte")
    await whatsapp.send_text(user, text)


async def choose_language(user: str, langue: str) -> None:
    """Après un appui sur Wolof / Pulaar / Français : bienvenue dans la langue, puis menu."""
    await memory.update_profile(user, langue=langue, accueilli=True)
    campaigns.subscribe(user, langue)
    await say(user, textes.BIENVENUE[langue], langue)
    await send_menu(user, langue)


async def send_emergency(user: str, langue: str) -> None:
    await images.send(user, "urgence")
    await say(user, textes.URGENCE[langue], langue)
    await send_samu_contact(user)


async def send_samu_contact(user: str) -> None:
    await whatsapp.send_contact(user, "SAMU", get_settings().samu_number)


async def run_command(user: str, cmd: str, langue: str) -> None:
    if cmd == "menu":
        await send_menu(user, langue)
    elif cmd == "stop":
        campaigns.unsubscribe(user)
        await say(user, textes.DESINSCRIT[langue], langue)
    elif cmd == "alerte":
        campaigns.subscribe(user, langue)
        await say(user, textes.REINSCRIT[langue], langue)
