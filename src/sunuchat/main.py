"""Serveur FastAPI : /health, page de confidentialité et webhook WhatsApp.

Chemin d'un message :
  Meta → POST /webhook → (vocal ? speech.transcribe) → agent.answer
  → réponse en vocal (speech.text_to_voice_note) si l'utilisateur parle en vocal, sinon en texte
  (+ carte du poste de santé / bouton « Envoyer la position » / fiche SAMU)
Premier message : carte de choix de la langue ; ensuite menu en boutons (onboarding.py).

Lancer :  uv run sunuchat   (ou  uv run uvicorn sunuchat.main:app --reload --port 8000)
"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from sunuchat import (
    campaigns,
    images,
    knowledge,
    memory,
    onboarding,
    reminders,
    speech,
    textes,
    whatsapp,
)
from sunuchat.config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s : %(message)s")
log = logging.getLogger("sunuchat")

MSG_ERREUR = "Désolé, je n'arrive pas à répondre pour le moment. Réessaie dans quelques minutes."
MSG_VOCAL_KO = (
    "Désolé, je n'arrive pas à écouter les vocaux pour le moment. Écris-moi ta question, "
    "je te réponds tout de suite."
)
MSG_TYPE_NON_GERE = (
    "Je comprends les messages écrits, les vocaux et la position partagée. "
    "Écris-moi, envoie un vocal, ou écris « menu »."
)
MSG_POSITION = "Je viens de partager ma position. Où est le poste de santé le plus proche ?"
MSG_DEMANDE_POSITION = (
    "📍 Appuie sur « Envoyer la position » : je te trouve le poste de santé le plus proche."
)

# Utilisateurs qui parlent en vocal : on leur répond en vocal seulement, y compris après le
# partage de leur position. Un message écrit les repasse en mode texte.
_voice_users: set[str] = set()

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


async def _send_reply(user: str, reply: str, langue: str, voice: bool) -> None:
    """Vocal seul pour qui parle en vocal (le texte en plus gâcherait l'écoute), texte sinon.
    Si la synthèse vocale échoue, le texte part quand même."""
    if voice and langue in speech.TTS_VOICES:
        try:
            ogg = await speech.text_to_voice_note(reply, langue)
            await whatsapp.send_audio(user, ogg)
            return
        except Exception:
            log.exception("Vocal de réponse non envoyé : réponse envoyée en texte")
    await whatsapp.send_text(user, reply)


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
        options: dict = {}  # position partagée, langue imposée par le menu
        image = None
        if msg_type == "interactive":
            choice = whatsapp.interactive_reply_id(message)
            if choice.startswith("langue_"):
                await onboarding.choose_language(user, choice.removeprefix("langue_"))
                return
            if choice == "menu_urgence":
                await onboarding.send_emergency(user, langue)
                return
            if choice not in textes.QUESTIONS_MENU:
                return
            # Choix dans le menu : réponse en vocal en wolof et pulaar (pour qui ne lit pas).
            text, voice = textes.QUESTIONS_MENU[choice], langue in speech.TTS_VOICES
            options["language"] = langue
            image = textes.IMAGES_MENU.get(choice)
            if voice:
                _voice_users.add(user)
        elif msg_type == "text":
            text, voice = message["text"]["body"], False
            _voice_users.discard(user)
            if cmd := onboarding.command(text):
                await onboarding.run_command(user, cmd, langue)
                return
        elif msg_type == "location":
            loc = message["location"]
            options["position"] = {"lat": loc["latitude"], "lon": loc["longitude"]}
            text, voice = MSG_POSITION, user in _voice_users
        elif msg_type == "audio":
            voice = True
            _voice_users.add(user)
            try:
                audio = await whatsapp.download_media(message["audio"]["id"])
                text = await speech.transcribe(audio)
            except Exception:
                log.exception("Vocal non transcrit")
                await whatsapp.send_text(user, MSG_VOCAL_KO)
                return
            if not text:
                await whatsapp.send_text(user, MSG_VOCAL_KO)
                return
            log.info("Transcription : %s", text)
        else:
            await whatsapp.send_text(user, MSG_TYPE_NON_GERE)
            return

        if not profile.get("accueilli"):
            # Premier contact : choix de la langue en boutons. Une simple salutation n'appelle
            # pas d'autre réponse ; une vraie question est traitée tout de suite après.
            await memory.update_profile(user, accueilli=True)
            await onboarding.send_language_card(user)
            if msg_type == "text" and onboarding.is_greeting(text):
                return

        try:
            reply, langue = await agent.answer(user, text, **options)
        except Exception:
            log.exception("Erreur de l'agent")
            await whatsapp.send_text(user, MSG_ERREUR)
            return

        if image:
            await images.send(user, image)
        await _send_reply(user, reply, langue, voice)
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
<p>SunuChat est un assistant santé sur WhatsApp, en wolof, pulaar et français, réalisé dans le
cadre d'un hackathon. Il donne des informations générales sur le paludisme, la dengue et la
vaccination des enfants, à partir de sources officielles. Il ne remplace pas un avis médical.</p>
<h2>Données collectées</h2>
<p>Ton numéro WhatsApp, les messages et vocaux que tu nous envoies, et les informations que tu
choisis de donner (localité, date de naissance d'un enfant, rappels de médicaments).</p>
<h2>Utilisation</h2>
<p>Ces données servent uniquement à te répondre, à te rappeler tes médicaments et les vaccins
de tes enfants, à t'envoyer des alertes de prévention pendant l'hivernage (écris « stop » pour
les arrêter) et à garder le fil de la conversation. Elles ne sont ni vendues ni utilisées
pour de la publicité.</p>
<h2>Services utilisés</h2>
<p>Les messages passent par WhatsApp (Meta). Les vocaux sont transcrits et lus par l'API vocale
Kiriku. Les réponses sont générées avec Google Gemini.</p>
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
