import asyncio
from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from sunuchat import campaigns, images, onboarding, reminders, textes, whatsapp
from sunuchat.config import get_settings


def _capture(monkeypatch):
    sent = []

    async def fake_post(payload):
        sent.append(payload)
        return {}

    monkeypatch.setattr(whatsapp, "_post", fake_post)
    return sent


def test_buttons_list_contact_template_payloads(monkeypatch):
    sent = _capture(monkeypatch)
    asyncio.run(whatsapp.send_buttons("221", "Choisis", textes.BOUTONS_LANGUE, "img1"))
    asyncio.run(whatsapp.send_list("221", "Menu", "Tànnal", textes.MENU["wolof"]))
    asyncio.run(whatsapp.send_contact("221", "SAMU", "1515"))
    asyncio.run(whatsapp.send_template("221", "rappel_vaccin", "fr", ["6 semaines"], "img2"))
    buttons, menu, contact, template = sent
    assert [b["reply"]["id"] for b in buttons["interactive"]["action"]["buttons"]] == [
        "langue_wolof",
        "langue_pulaar",
        "langue_francais",
    ]
    assert buttons["interactive"]["header"]["image"]["id"] == "img1"
    rows = menu["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) == 5 and rows[0]["id"] == "menu_palu"
    assert contact["contacts"][0]["phones"][0]["phone"] == "1515"
    assert template["template"]["components"][1]["parameters"][0]["text"] == "6 semaines"


def test_whatsapp_limits_respected_in_texts():
    for _, title in textes.BOUTONS_LANGUE:
        assert len(title) <= 20
    for langue in textes.LANGUES:
        assert len(textes.MENU_BOUTON[langue]) <= 20
        for rid, title, desc in textes.MENU[langue]:
            assert len(title) <= 24 and len(desc) <= 72
            assert rid in textes.QUESTIONS_MENU or rid == "menu_urgence"
        assert textes.BIENVENUE[langue] and textes.URGENCE[langue]


def test_greetings_and_commands():
    assert onboarding.is_greeting("Salam")
    assert onboarding.is_greeting("Asalaa maalekum")
    assert onboarding.is_greeting("No mbaɗ-ɗaa ?")
    assert not onboarding.is_greeting(
        "Salam, mon enfant a de la fièvre depuis hier soir, que faire ?"
    )
    assert onboarding.command("Menu") == "menu"
    assert onboarding.command("STOP") == "stop"
    assert onboarding.command("j'ai mal") is None


def test_vaccine_reminders_scheduled_day_before(monkeypatch):
    monkeypatch.setattr(reminders, "_scheduler", AsyncIOScheduler(timezone="Africa/Dakar"))
    birth = date(2026, 9, 1)
    ids = reminders.add_vaccine_reminders("221", birth, today=date(2026, 10, 10))
    # Naissance et 6 semaines (12/10 → rappel le 11/10) : seules les dates futures comptent.
    assert len(ids) == 5
    job = reminders.scheduler().get_job(f"221-vaccin-{birth.isoformat()}-42")
    assert job.trigger.run_date.strftime("%Y-%m-%d %H:%M") == "2026-10-12 10:00"


def test_subscribers(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    campaigns.subscribe("221a", "wolof")
    campaigns.subscribe("221b", "pulaar")
    campaigns.unsubscribe("221a")
    assert campaigns.active_subscribers() == ["221b"]
    campaigns.subscribe("221a", "wolof")
    assert sorted(campaigns.active_subscribers()) == ["221a", "221b"]


def test_seasonal_alert_needs_template(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(get_settings(), "template_alerte_saison", "")
    assert asyncio.run(campaigns.send_seasonal_alert(["221"])) == 0


def test_images_uploaded_once(monkeypatch, tmp_path):
    folder = tmp_path / "images"
    folder.mkdir()
    (folder / "paludisme.jpg").write_bytes(b"jpg")
    monkeypatch.setattr(images, "IMAGES_DIR", folder)
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    uploads = []

    async def fake_upload(data, mime, name):
        uploads.append((mime, name))
        return "media-1"

    monkeypatch.setattr(whatsapp, "upload_media", fake_upload)
    assert asyncio.run(images.media_id("paludisme")) == "media-1"
    assert asyncio.run(images.media_id("paludisme")) == "media-1"
    assert uploads == [("image/jpeg", "paludisme.jpg")]
    assert asyncio.run(images.media_id("dengue")) is None
