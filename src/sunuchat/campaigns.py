"""Alertes de saison (hivernage : paludisme et dengue) aux personnes inscrites.

Toute personne qui choisit sa langue à l'accueil est inscrite ; « stop » la désinscrit,
« alerte » la réinscrit. Les alertes partent forcément par un modèle de message approuvé
par Meta (TEMPLATE_ALERTE_SAISON), car elles arrivent hors de la fenêtre de 24 h.

Envoi automatique les 15 juillet et 15 septembre à 10 h (début et pic de l'hivernage),
ou à la main :
    uv run python -m sunuchat.campaigns              # tous les inscrits
    uv run python -m sunuchat.campaigns 22177xxxxxxx # un seul numéro, pour tester
"""

import asyncio
import logging
import sqlite3
import sys
from datetime import datetime

from sunuchat.config import get_settings

log = logging.getLogger(__name__)

# (mois, jour) des envois automatiques.
SEASONAL_DATES = [(7, 15), (9, 15)]


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(get_settings().data_dir / "abonnes.db")
    con.execute(
        "CREATE TABLE IF NOT EXISTS abonnes (numero TEXT PRIMARY KEY, langue TEXT, "
        "inscrit_le TEXT, actif INTEGER NOT NULL DEFAULT 1)"
    )
    return con


def subscribe(numero: str, langue: str) -> None:
    with _db() as con:
        con.execute(
            "INSERT INTO abonnes (numero, langue, inscrit_le, actif) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(numero) DO UPDATE SET langue = excluded.langue, actif = 1",
            (numero, langue, datetime.now().isoformat(timespec="seconds")),
        )


def unsubscribe(numero: str) -> None:
    with _db() as con:
        con.execute("UPDATE abonnes SET actif = 0 WHERE numero = ?", (numero,))


def active_subscribers() -> list[str]:
    with _db() as con:
        return [r[0] for r in con.execute("SELECT numero FROM abonnes WHERE actif = 1")]


async def send_seasonal_alert(numeros: list[str] | None = None) -> int:
    """Envoie l'alerte d'hivernage ; renvoie le nombre de messages partis."""
    from sunuchat import images, whatsapp

    s = get_settings()
    if not s.template_alerte_saison:
        log.warning("Alerte de saison non envoyée : TEMPLATE_ALERTE_SAISON est vide")
        return 0
    image_id = await images.media_id("hivernage")
    sent = 0
    for numero in numeros or active_subscribers():
        try:
            await whatsapp.send_template(
                numero, s.template_alerte_saison, s.template_language, image_id=image_id
            )
            sent += 1
        except Exception:
            log.exception("Alerte non envoyée à %s", numero)
        await asyncio.sleep(0.2)  # rester sous la limite de débit de Meta
    log.info("Alerte de saison envoyée à %d personne(s)", sent)
    return sent


def schedule_seasonal_alerts() -> None:
    from sunuchat import reminders

    for month, day in SEASONAL_DATES:
        reminders.scheduler().add_job(
            send_seasonal_alert,
            "cron",
            month=month,
            day=day,
            hour=10,
            id=f"alerte-hivernage-{month:02d}-{day:02d}",
            replace_existing=True,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(send_seasonal_alert(sys.argv[1:] or None))
