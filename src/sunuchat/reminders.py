"""Rappels sur WhatsApp : médicaments (à heure fixe) et vaccins (la veille de chaque
rendez-vous du PEV), plus les alertes de saison (voir campaigns.py).

Les rappels sont gardés dans data/reminders.db : ils survivent à un redémarrage du serveur.
Attention : hors de la fenêtre de 24 h après le dernier message de l'utilisateur, WhatsApp
exige un modèle de message approuvé. Le rappel texte simple ne marche que dans ces 24 h.
"""

import logging
from datetime import date, datetime, time, timedelta

from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sunuchat.config import get_settings

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


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


async def send_reminder(to: str, text: str) -> None:
    from sunuchat import whatsapp

    try:
        await whatsapp.send_text(to, f"⏰ Rappel : {text}")
    except Exception:
        log.exception("Rappel non envoyé à %s", to)


def add_reminder(to: str, medicament: str, heures: list[str], jours: int) -> list[str]:
    """Programme un rappel chaque jour aux heures données (HH:MM), pendant `jours` jours."""
    end = datetime.now() + timedelta(days=jours)
    ids = []
    for heure in heures:
        hour, minute = (int(x) for x in heure.split(":"))
        job = scheduler().add_job(
            send_reminder,
            "cron",
            hour=hour,
            minute=minute,
            end_date=end,
            args=[to, f"c'est l'heure de prendre {medicament}."],
            id=f"{to}-{medicament}-{heure}".replace(" ", "_"),
            replace_existing=True,
        )
        ids.append(job.id)
    return ids


def list_reminders(to: str) -> list[str]:
    return [j.id for j in scheduler().get_jobs() if j.id.startswith(f"{to}-")]


# ---------- Rappels de vaccins ----------
VACCINE_REMINDER_TIME = time(10, 0)  # la veille du rendez-vous, à 10 h (heure de Dakar)


async def send_vaccine_reminder(to: str, age: str, due: str, vaccins: str) -> None:
    """Rappel de vaccin : modèle Meta si configuré (obligatoire hors fenêtre de 24 h),
    sinon simple texte (ne passe que si l'utilisateur a écrit dans les dernières 24 h)."""
    from sunuchat import images, whatsapp

    s = get_settings()
    jour = date.fromisoformat(due).strftime("%d/%m/%Y")
    try:
        if s.template_rappel_vaccin:
            await whatsapp.send_template(
                to,
                s.template_rappel_vaccin,
                s.template_language,
                [age, jour, vaccins],
                image_id=await images.media_id("vaccins"),
            )
        else:
            await whatsapp.send_text(
                to,
                f"💉 Rappel : demain {jour}, ton enfant a rendez-vous pour ses vaccins "
                f"de {age} ({vaccins}). Va au poste de santé avec son carnet.",
            )
    except Exception:
        log.exception("Rappel de vaccin non envoyé à %s", to)


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
