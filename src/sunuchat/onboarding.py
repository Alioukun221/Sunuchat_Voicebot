"""Messages fixes en vocal (erreurs, inscription aux alertes) et fiche SAMU. La présentation
du bot et la liste de ce qu'il sait faire sont dans le prompt de l'agent (agent.py) : il
répond lui-même à « Salam » ou « qu'est-ce que tu sais faire ? ». Les textes sont dans
textes.py.
"""

import logging

from sunuchat import campaigns, memory, speech, textes, whatsapp
from sunuchat.config import get_settings

log = logging.getLogger(__name__)

_COMMANDS = {
    "stop": "stop",
    "arret": "stop",
    "arrêt": "stop",
    "alerte": "alerte",
    "alertes": "alerte",
    # Changer de langue : le nom de la langue, dit ou écrit seul.
    "wolof": "langue:wolof",
    "pulaar": "langue:pulaar",
    "peul": "langue:pulaar",
    "seereer": "langue:serer",
    "sereer": "langue:serer",
    "serer": "langue:serer",
    "sérère": "langue:serer",
    "sérèr": "langue:serer",
    "sérer": "langue:serer",
}
_voice_cache: dict[str, bytes] = {}


def command(text: str) -> str | None:
    return _COMMANDS.get((text or "").strip().strip(".!").lower())


def language_of(profile: dict) -> str:
    langue = profile.get("langue") or get_settings().default_language
    return langue if langue in textes.LANGUES else "wolof"


async def say(user: str, text: str, langue: str) -> None:
    """Vocal (les textes fixes sont mis en cache) ; texte seulement si la voix échoue."""
    try:
        ogg = _voice_cache.get(text) or await speech.text_to_voice_note(text, langue)
        _voice_cache[text] = ogg
        await whatsapp.send_audio(user, ogg)
        return
    except Exception:
        log.exception("Vocal non généré : envoi en texte")
    await whatsapp.send_text(user, text)


async def say_fixed(user: str, texts: dict[str, str], langue: str) -> None:
    """Message fixe de textes.py, en wolof si la langue n'a pas encore sa version."""
    await say(user, *textes.texte(texts, langue))


async def send_samu_contact(user: str) -> None:
    await whatsapp.send_contact(user, "SAMU", get_settings().samu_number)


async def run_command(user: str, cmd: str, langue: str) -> None:
    if cmd == "stop":
        campaigns.unsubscribe(user)
        await say_fixed(user, textes.DESINSCRIT, langue)
    elif cmd == "alerte":
        campaigns.subscribe(user, langue)
        await say_fixed(user, textes.REINSCRIT, langue)
    elif cmd.startswith("langue:"):
        langue = cmd.removeprefix("langue:")
        await memory.update_profile(user, langue=langue)
        await say_fixed(user, textes.LANGUE_CHOISIE, langue)
