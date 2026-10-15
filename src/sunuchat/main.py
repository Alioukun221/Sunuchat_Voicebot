"""Serveur FastAPI : /health, page de confidentialité et webhook WhatsApp.

Chemin d'un message (vocal, texte, position ou photo d'ordonnance) :
  Meta → POST /webhook → (vocal ? speech.transcribe ; photo ? ordonnance.read) → agent.answer
  → réponse toujours en vocal, en wolof, en pulaar ou en sérère (les utilisateurs ne savent
  souvent pas lire), puis la carte du poste de santé, le bouton de partage de position ou la
  fiche SAMU.

Lancer :  uv run sunuchat   (ou  uv run uvicorn sunuchat.main:app --reload --port 8000)
"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from sunuchat import (
    campaigns,
    knowledge,
    memory,
    onboarding,
    ordonnance,
    reminders,
    speech,
    textes,
    whatsapp,
)
from sunuchat.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
log = logging.getLogger("sunuchat")

MSG_POSITION = "Je viens de partager ma position. Où est le poste de santé le plus proche ?"
MSG_DEMANDE_POSITION = "📍"
# Formats lus comme une ordonnance.
ORDONNANCE_TYPES = ("image/jpeg", "image/png", "image/webp", "application/pdf")

# Meta peut renvoyer le même message plusieurs fois : on ne le traite qu'une fois.
_seen_ids: set[str] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Index RAG chargé une fois pour toutes (et non à chaque message).
    await asyncio.to_thread(knowledge.load_index)
    campaigns.schedule_seasonal_alerts()
    reminders.start()
    yield
    reminders.shutdown()


app = FastAPI(title="SunuChat", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/webhook")
async def verify_webhook(
    mode: str = Query("", alias="hub.mode"),
    token: str = Query("", alias="hub.verify_token"),
    challenge: str = Query("", alias="hub.challenge"),
) -> PlainTextResponse:
    """Vérification du webhook par Meta (au moment de « Vérifier et enregistrer »)."""
    expected = get_settings().whatsapp_verify_token
    if mode == "subscribe" and expected and token == expected:
        return PlainTextResponse(challenge)
    raise HTTPException(status_code=403, detail="Jeton de vérification invalide")


@app.post("/webhook")
async def receive_webhook(request: Request, background: BackgroundTasks) -> dict:
    body = await request.body()
    if not whatsapp.verify_signature(body, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(status_code=403, detail="Signature invalide")
    payload = await request.json()
    for message in whatsapp.parse_messages(payload):
        msg_id = message.get("id")
        if msg_id in _seen_ids:
            continue
        _seen_ids.add(msg_id)
        # On répond 200 tout de suite à Meta ; le traitement se fait en arrière-plan.
        background.add_task(handle_message, message)
    return {"status": "ok"}


async def _send_actions(user: str, actions: list[dict]) -> None:
    """Carte du poste de santé le plus proche, bouton « Envoyer la position »…"""
    for action in actions:
        try:
            if action["type"] == "location":
                await whatsapp.send_location(
                    user, action["lat"], action["lon"], action["nom"], action["adresse"]
                )
            elif action["type"] == "location_request":
                await whatsapp.send_location_request(user, MSG_DEMANDE_POSITION)
            elif action["type"] == "samu":
                await onboarding.send_samu_contact(user)
        except Exception:
            log.exception("Action WhatsApp non envoyée : %s", action["type"])


async def _send_reply(user: str, reply: str, langue: str) -> None:
    """Toujours en vocal : la plupart des utilisateurs ne lisent pas. Texte seulement si la
    synthèse vocale échoue."""
    try:
        ogg = await speech.text_to_voice_note(reply, langue)
        await whatsapp.send_audio(user, ogg)
        return
    except Exception:
        log.exception("Vocal de réponse non envoyé : réponse envoyée en texte")
    await whatsapp.send_text(user, reply)


async def _read_prescription(user: str, message: dict, langue: str) -> str | None:
    """Photo (ou PDF) d'ordonnance → texte pour l'agent ; None si elle est illisible."""
    media = message.get("image") or message.get("document") or {}
    data = await whatsapp.download_media(media["id"])
    result = await ordonnance.read(data, media.get("mime_type", "image/jpeg"))
    if not (result.est_ordonnance and result.lisible and result.medicaments):
        await onboarding.say_fixed(user, textes.ORDONNANCE_ILLISIBLE, langue)
        return None
    return ordonnance.as_text(result)


async def _keep_typing(message_id: str) -> None:
    """WhatsApp retire « en train d'écrire » au bout de 25 s : on le relance toutes les 20 s."""
    while True:
        await whatsapp.mark_read_and_typing(message_id)
        await asyncio.sleep(20)


async def _profile(user: str) -> dict:
    try:
        return await memory.get_profile(user)
    except Exception:
        log.exception("Profil illisible")
        return {"accueilli": True}


async def handle_message(message: dict) -> None:
    from sunuchat import agent  # import tardif : ADK est lourd à charger

    user = message["from"]
    msg_type = message.get("type")
    typing = asyncio.create_task(_keep_typing(message["id"]))
    try:
        profile = await _profile(user)
        langue = onboarding.language_of(profile)
        options: dict = {}
        mime = (message.get("document") or {}).get("mime_type", "")
        if msg_type == "text":
            text = message["text"]["body"]
            if cmd := onboarding.command(text):
                await onboarding.run_command(user, cmd, langue)
                return
        elif msg_type == "location":
            loc = message["location"]
            options["position"] = {"lat": loc["latitude"], "lon": loc["longitude"]}
            text = MSG_POSITION
        elif msg_type == "audio":
            try:
                audio = await whatsapp.download_media(message["audio"]["id"])
                if langue == "serer":  # Kiriku transcrit mieux le sérère quand on le lui dit
                    text = await speech.transcribe(audio, "serer")
                else:
                    text = await speech.transcribe(audio)
            except Exception:
                log.exception("Vocal non transcrit")
                text = ""
            if not text:
                await onboarding.say_fixed(user, textes.VOCAL_KO, langue)
                return
            log.info("Transcription : %s", text)
            if cmd := onboarding.command(text):  # « stop » ou « alerte » dit à voix haute
                await onboarding.run_command(user, cmd, langue)
                return
        elif msg_type == "image" or (msg_type == "document" and mime in ORDONNANCE_TYPES):
            try:
                text = await _read_prescription(user, message, langue)
            except Exception:
                log.exception("Ordonnance non lue")
                await onboarding.say_fixed(user, textes.ORDONNANCE_ILLISIBLE, langue)
                return
            if not text:
                return
            options["keep_language"] = True
        else:
            await onboarding.say_fixed(user, textes.TYPE_NON_GERE, langue)
            return

        if not profile.get("accueilli"):
            # Premier contact : inscription aux alertes de saison (« stop » pour arrêter).
            await memory.update_profile(user, accueilli=True)
            campaigns.subscribe(user, langue)

        try:
            reply, langue = await agent.answer(user, text, **options)
        except Exception:
            log.exception("Erreur de l'agent")
            await onboarding.say_fixed(user, textes.ERREUR, langue)
            return

        await _send_reply(user, reply, langue)
        # Après la réponse, pour que « appuie sur le bouton en dessous » ait du sens.
        await _send_actions(user, agent.pop_actions(user))
    except Exception:
        log.exception("Erreur inattendue")
    finally:
        typing.cancel()
        with suppress(asyncio.CancelledError):
            await typing


PRIVACY_HTML = """<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SunuChat - Politique de confidentialité</title>
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:2rem auto;padding:0 1rem;
line-height:1.6;color:#222}h1{font-size:1.6rem}h2{font-size:1.15rem;margin-top:1.6rem}</style>
</head><body>
<h1>SunuChat - Politique de confidentialité</h1>
<p>SunuChat est un assistant santé vocal sur WhatsApp, en wolof, en pulaar et en sérère, réalisé
dans le cadre d'un hackathon. Il donne des informations générales de santé (paludisme, dengue,
vaccination des enfants et autres maladies), à partir de sources officielles et de recherches
sur le web, et explique les ordonnances. Il ne remplace pas un avis médical.</p>
<h2>Données collectées</h2>
<p>Ton numéro WhatsApp, les messages, vocaux et photos d'ordonnance que tu nous envoies, et les
informations que tu choisis de donner (localité, date de naissance d'un enfant, rappels de
médicaments).</p>
<h2>Utilisation</h2>
<p>Ces données servent uniquement à te répondre, à te rappeler tes médicaments et les vaccins
de tes enfants, à t'envoyer des alertes de prévention pendant l'hivernage (dis ou écris
« stop » pour les arrêter) et à garder le fil de la conversation. Elles ne sont ni vendues
ni utilisées pour de la publicité.</p>
<h2>Services utilisés</h2>
<p>Les messages passent par WhatsApp (Meta). Les vocaux sont transcrits et lus par l'API vocale
Kiriku. Les réponses et la lecture des ordonnances sont faites avec Google Gemini.</p>
<h2>Conservation et suppression</h2>
<p>Les données sont gardées pendant la durée du projet. Pour les faire supprimer, écris à
l'équipe SunuChat : elles seront effacées sous 7 jours.</p>
</body></html>"""


@app.get("/confidentialite", response_class=HTMLResponse)
async def confidentialite() -> str:
    return PRIVACY_HTML


def run() -> None:
    import uvicorn

    uvicorn.run("sunuchat.main:app", host="0.0.0.0", port=8000)
