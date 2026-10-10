"""Rappels sur WhatsApp : médicaments (à heure fixe) et vaccins (la veille de chaque
rendez-vous du PEV), plus les alertes de saison (voir campaigns.py).

Chaque rappel part en vocal, dans la langue de l'utilisateur, posé sur une courte animation
(assets/animations/<nom>.mp4) : une petite vidéo que l'on comprend sans savoir lire. Sans
animation, il part en simple vocal.

Les rappels sont gardés dans data/reminders.db : ils survivent à un redémarrage du serveur.
Attention : hors de la fenêtre de 24 h après le dernier message de l'utilisateur, WhatsApp
n'accepte qu'un modèle de message approuvé par Meta (texte ou image, pas de vocal). Pour les
vaccins, le modèle TEMPLATE_RAPPEL_VACCIN prend alors le relais s'il est configuré.
"""

import logging
from datetime import date, datetime, time, timedelta

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sunuchat import memory, onboarding, speech, textes, whatsapp
from sunuchat.config import ROOT, get_settings

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
ANIMATIONS_DIR = ROOT / "assets" / "animations"


def scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        db = get_settings().data_dir / "reminders.db"
        _scheduler = AsyncIOScheduler(
            jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{db}")},
            timezone="Africa/Dakar",
        )
    return _scheduler


def start() -> None:
    if not scheduler().running:
        scheduler().start()


def shutdown() -> None:
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)


async def send_voice_reminder(to: str, texts: dict[str, str], animation: str, **params) -> None:
    """Rappel lu à voix haute dans la langue de l'utilisateur, sur l'animation si elle existe.
    Lève une exception si WhatsApp refuse (fenêtre de 24 h fermée)."""
    try:
        langue = onboarding.language_of(await memory.get_profile(to))
    except Exception:
        log.exception("Profil illisible : rappel en wolof")
        langue = "wolof"
    wav = await speech.synthesize(texts[langue].format(**params), langue)
    path = ANIMATIONS_DIR / f"{animation}.mp4"
    if path.exists():
        try:
            video = await speech.voice_video(wav, path)
        except Exception:
            log.exception("Animation non générée : rappel en simple vocal")
        else:
            await whatsapp.send_video(to, video)
            return
    await whatsapp.send_audio(to, await speech.wav_to_ogg(wav))


async def send_medication_reminder(to: str, medicament: str) -> None:
    try:
        await send_voice_reminder(
            to, textes.RAPPEL_MEDICAMENT, "rappel_medicament", medicament=medicament
        )
    except Exception:
        log.exception("Rappel de médicament non envoyé à %s", to)


async def send_reminder(to: str, text: str) -> None:
    """Anciens rappels déjà programmés (texte « c'est l'heure de prendre X. »)."""
    medicament = text.removeprefix("c'est l'heure de prendre ").rstrip(".")
    await send_medication_reminder(to, medicament)


def add_reminder(to: str, medicament: str, heures: list[str], jours: int) -> list[str]:
    """Programme un rappel chaque jour aux heures données (HH:MM), pendant `jours` jours."""
    end = datetime.now() + timedelta(days=jours)
    ids = []
    for heure in heures:
        hour, minute = (int(x) for x in heure.split(":"))
        job = scheduler().add_job(
            send_medication_reminder,
            "cron",
            hour=hour,
            minute=minute,
            end_date=end,
            args=[to, medicament],
            id=f"{to}-{medicament}-{heure}".replace(" ", "_"),
            replace_existing=True,
        )
        ids.append(job.id)
    return ids


def list_reminders(to: str) -> list[str]:
    return [j.id for j in scheduler().get_jobs() if j.id.startswith(f"{to}-")]


# ---------- Rappels de vaccins ----------
VACCINE_REMINDER_TIME = time(10, 0)  


async def send_vaccine_reminder(to: str, age: str, due: str, vaccins: str) -> None:
    """Rappel de vaccin en vocal sur animation ; hors fenêtre de 24 h, modèle Meta s'il est
    configuré (WhatsApp n'accepte rien d'autre)."""
    from sunuchat import images

    s = get_settings()
    jour = date.fromisoformat(due).strftime("%d/%m/%Y")
    try:
        await send_voice_reminder(to, textes.RAPPEL_VACCIN, "rappel_vaccin", jour=jour, age=age)
        return
    except Exception:
        log.exception("Rappel de vaccin vocal non envoyé à %s", to)
    if not s.template_rappel_vaccin:
        return
    try:
        await whatsapp.send_template(
            to,
            s.template_rappel_vaccin,
            s.template_language,
            [age, jour, vaccins],
            image_id=await images.media_id("vaccins"),
        )
    except Exception:
        log.exception("Modèle de rappel de vaccin non envoyé à %s", to)


def add_vaccine_reminders(to: str, birth: date, today: date | None = None) -> list[str]:
    """Programme un rappel la veille de chaque vaccin à venir de l'enfant né le `birth`."""
    from sunuchat import rules

    today = today or date.today()
    ids = []
    for dose in rules.PEV_CALENDAR:
        due = birth + timedelta(days=dose.age_days)
        run = datetime.combine(due - timedelta(days=1), VACCINE_REMINDER_TIME)
        if run.date() <= today:
            continue
        job = scheduler().add_job(
            send_vaccine_reminder,
            "date",
            run_date=run,
            args=[to, dose.age_label, due.isoformat(), dose.vaccines],
            id=f"{to}-vaccin-{birth.isoformat()}-{dose.age_days}",
            replace_existing=True,
        )
        ids.append(job.id)
    return ids
