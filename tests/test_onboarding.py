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
    boutons = [("langue_wolof", "Wolof"), ("langue_pulaar", "Pulaar")]
    lignes = [("menu_palu", "Sibiru", "Màndarga yi"), ("menu_dengue", "Dengue", "Màndarga yi")]
    asyncio.run(whatsapp.send_buttons("221", "Choisis", boutons, "img1"))
    asyncio.run(whatsapp.send_list("221", "Menu", "Tànnal", lignes))
    asyncio.run(whatsapp.send_contact("221", "SAMU", "1515"))
    asyncio.run(whatsapp.send_template("221", "rappel_vaccin", "fr", ["6 semaines"], "img2"))
    buttons, menu, contact, template = sent
    assert [b["reply"]["id"] for b in buttons["interactive"]["action"]["buttons"]] == [
        "langue_wolof",
        "langue_pulaar",
    ]
    assert buttons["interactive"]["header"]["image"]["id"] == "img1"
    rows = menu["interactive"]["action"]["sections"][0]["rows"]
    assert len(rows) == 2 and rows[0]["id"] == "menu_palu"
    assert contact["contacts"][0]["phones"][0]["phone"] == "1515"
    assert template["template"]["components"][1]["parameters"][0]["text"] == "6 semaines"


def test_texts_only_in_wolof_and_pulaar():
    assert textes.LANGUES == ("wolof", "pulaar")
    for nom in (
        "ERREUR",
        "VOCAL_KO",
        "TYPE_NON_GERE",
        "ORDONNANCE_ILLISIBLE",
        "RAPPEL_MEDICAMENT",
        "RAPPEL_VACCIN",
        "DESINSCRIT",
        "REINSCRIT",
    ):
        assert set(getattr(textes, nom)) == {"wolof", "pulaar"}, nom
    for langue in textes.LANGUES:
        assert "{medicament}" in textes.RAPPEL_MEDICAMENT[langue]
        assert "{jour}" in textes.RAPPEL_VACCIN[langue]


def test_commands_and_language():
    assert onboarding.command("STOP") == "stop"
    assert onboarding.command("Alerte.") == "alerte"
    assert onboarding.command("j'ai mal") is None
    # Un ancien profil en français repasse en wolof : SunuChat ne parle plus français.
    assert onboarding.language_of({"langue": "francais"}) == "wolof"
    assert onboarding.language_of({"langue": "pulaar"}) == "pulaar"


def _reminder_fakes(monkeypatch, sent, langue="pulaar"):
    async def fake_profile(user):
        return {"langue": langue}

    async def fake_synthesize(text, language):
        sent.append(("voix", language, text))
        return b"wav"

    async def fake_video(wav, animation):
        sent.append(("animation", animation.name))
        return b"mp4"

    async def fake_send_video(to, mp4):
        sent.append(("video", to))

    monkeypatch.setattr(reminders.memory, "get_profile", fake_profile)
    monkeypatch.setattr(reminders.speech, "synthesize", fake_synthesize)
    monkeypatch.setattr(reminders.speech, "voice_video", fake_video)
    monkeypatch.setattr(reminders.whatsapp, "send_video", fake_send_video)


def test_medication_reminder_is_spoken_on_animation(monkeypatch):
    sent = []
    _reminder_fakes(monkeypatch, sent)
    asyncio.run(reminders.send_medication_reminder("221", "Coartem"))
    texte = textes.RAPPEL_MEDICAMENT["pulaar"].format(medicament="Coartem")
    assert sent == [
        ("voix", "pulaar", texte),
        ("animation", "rappel_medicament.mp4"),
        ("video", "221"),
    ]


def test_old_text_reminder_jobs_still_spoken(monkeypatch):
    sent = []
    _reminder_fakes(monkeypatch, sent, langue="wolof")
    asyncio.run(reminders.send_reminder("221", "c'est l'heure de prendre Coartem."))
    assert sent[0] == (
        "voix",
        "wolof",
        textes.RAPPEL_MEDICAMENT["wolof"].format(medicament="Coartem"),
    )


def test_vaccine_reminder_falls_back_to_template_outside_window(monkeypatch):
    sent = []
    _reminder_fakes(monkeypatch, sent)

    async def refused(to, mp4):
        raise RuntimeError("131047 : fenêtre de 24 h fermée")

    async def fake_template(to, name, language, params, image_id=None):
        sent.append(("modele", name, params))

    async def no_image(name):
        return None

    monkeypatch.setattr(reminders.whatsapp, "send_video", refused)
    monkeypatch.setattr(reminders.whatsapp, "send_template", fake_template)
    monkeypatch.setattr(images, "media_id", no_image)
    monkeypatch.setattr(get_settings(), "template_rappel_vaccin", "rappel_vaccin")
    asyncio.run(reminders.send_vaccine_reminder("221", "6 semaines", "2026-10-12", "Penta 1"))
    assert sent[-1] == ("modele", "rappel_vaccin", ["6 semaines", "12/10/2026", "Penta 1"])


def test_prescription_text_for_agent():
    from sunuchat import ordonnance

    o = ordonnance.Ordonnance(
        est_ordonnance=True,
        lisible=True,
        medicaments=[
            ordonnance.Medicament(
                nom="Amoxicilline",
                dosage="500 mg",
                posologie="1 gélule matin et soir",
                duree="7 jours",
            ),
            ordonnance.Medicament(nom="Doliprane", incertain=True),
        ],
        consignes="Après le repas",
    )
    texte = ordonnance.as_text(o)
    assert "Amoxicilline : 500 mg, 1 gélule matin et soir, 7 jours" in texte
    assert "Doliprane (difficile à lire)" in texte
    assert "Consignes : Après le repas" in texte


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


def test_models_after_skips_failed_model():
    from sunuchat.config import Settings

    s = Settings(fallback_models="a, b ,a,,c")
    assert s.models_after("b") == ["a", "c"]


def test_model_fallback_tries_next_model(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from google.genai import types

    from sunuchat import agent

    calls = []

    async def generate_content(model, contents, config):
        calls.append(model)
        if model == "b":
            raise RuntimeError("429")
        return types.GenerateContentResponse(
            candidates=[
                types.Candidate(content=types.Content(role="model", parts=[types.Part(text="ok")]))
            ]
        )

    client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=generate_content))
    )
    monkeypatch.setattr(agent, "_genai_client", lambda: client)
    monkeypatch.setattr(agent.get_settings(), "fallback_models", "a,b,c")
    req = SimpleNamespace(model="a", contents=[], config=types.GenerateContentConfig())
    r = asyncio.run(agent.model_fallback(None, req, RuntimeError("429")))
    assert calls == ["b", "c"]
    assert r.content.parts[0].text == "ok"


def test_web_search_failure_does_not_break_answer(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from google.genai import types

    from sunuchat import agent

    async def quota(model, contents, config):
        raise RuntimeError("429")

    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=quota)))
    monkeypatch.setattr(agent, "_genai_client", lambda: client)
    req = SimpleNamespace(model="a", contents=[], config=types.GenerateContentConfig())
    r = asyncio.run(agent.web_fallback(None, req, RuntimeError("429")))
    assert "indisponible" in r.content.parts[0].text
