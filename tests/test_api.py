import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from sunuchat import main
from sunuchat.config import get_settings


@pytest.fixture
def client(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "whatsapp_verify_token", "secret-verif")
    monkeypatch.setattr(s, "whatsapp_app_secret", "app-secret")
    main._seen_ids.clear()

    async def known_user(user):
        return {"accueilli": True, "langue": "wolof"}

    monkeypatch.setattr(main, "_profile", known_user)
    with TestClient(main.app) as c:
        yield c


def _signed(payload: dict) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    sig = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    return body, {"X-Hub-Signature-256": f"sha256={sig}", "Content-Type": "application/json"}


def _text_payload(text: str, msg_id: str = "wamid.1") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "contacts": [{"wa_id": "221700000000", "profile": {"name": "Awa"}}],
                            "messages": [
                                {
                                    "from": "221700000000",
                                    "id": msg_id,
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                    }
                ]
            }
        ],
    }


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_webhook_verification_ok(client):
    r = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "secret-verif",
            "hub.challenge": "1234",
        },
    )
    assert r.status_code == 200
    assert r.text == "1234"


def test_webhook_verification_bad_token(client):
    r = client.get(
        "/webhook",
        params={"hub.mode": "subscribe", "hub.verify_token": "faux", "hub.challenge": "1234"},
    )
    assert r.status_code == 403


def test_privacy_page(client):
    r = client.get("/confidentialite")
    assert r.status_code == 200
    assert "confidentialité" in r.text


def test_post_rejects_bad_signature(client):
    body, headers = _signed(_text_payload("Salam"))
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64
    assert client.post("/webhook", content=body, headers=headers).status_code == 403


@pytest.fixture
def sent(monkeypatch):
    """Tout ce que le bot envoie sur WhatsApp. Un vocal est noté ("vocal", texte lu)."""
    out = []

    async def fake_send_text(to, text):
        out.append(("texte", text))

    async def fake_tts(text, langue):
        return f"{langue}:{text}".encode()

    async def fake_send_audio(to, ogg):
        out.append(("vocal", ogg.decode()))

    async def fake_send_location(to, lat, lon, name="", address=""):
        out.append(("carte", name))

    async def fake_send_location_request(to, text):
        out.append(("bouton position", text))

    async def fake_send_contact(to, name, phone):
        out.append(("fiche", phone))

    async def fake_typing(message_id):
        return None

    async def fake_download(media_id):
        return b"media"

    async def fake_transcribe(audio):
        return "Fan la poste de santé bi gën a jege nekk ?"

    monkeypatch.setattr(main.whatsapp, "send_text", fake_send_text)
    monkeypatch.setattr(main.speech, "text_to_voice_note", fake_tts)
    monkeypatch.setattr(main.whatsapp, "send_audio", fake_send_audio)
    monkeypatch.setattr(main.whatsapp, "send_location", fake_send_location)
    monkeypatch.setattr(main.whatsapp, "send_location_request", fake_send_location_request)
    monkeypatch.setattr(main.whatsapp, "send_contact", fake_send_contact)
    monkeypatch.setattr(main.whatsapp, "mark_read_and_typing", fake_typing)
    monkeypatch.setattr(main.whatsapp, "download_media", fake_download)
    monkeypatch.setattr(main.speech, "transcribe", fake_transcribe)
    main.onboarding._voice_cache.clear()
    return out


def _set_answer(monkeypatch, fn):
    from sunuchat import agent

    monkeypatch.setattr(agent, "answer", fn)
    return agent


def _post(client, payload):
    body, headers = _signed(payload)
    return client.post("/webhook", content=body, headers=headers)


def _media_payload(msg_id, kind, media):
    payload = _text_payload("", msg_id)
    msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.pop("text")
    msg.update({"type": kind, kind: media})
    return payload


def test_text_message_answered_in_voice_once(client, monkeypatch, sent):
    async def fake_answer(user, text):
        return f"réponse à {text}", "wolof"

    _set_answer(monkeypatch, fake_answer)
    assert _post(client, _text_payload("Salam")).status_code == 200
    # Le même message renvoyé par Meta n'est traité qu'une fois.
    _post(client, _text_payload("Salam"))
    assert sent == [("vocal", "wolof:réponse à Salam")]


def test_voice_falls_back_to_text_when_tts_fails(client, monkeypatch, sent):
    async def fake_answer(user, text):
        return "Jërëjëf.", "wolof"

    async def broken_tts(text, langue):
        raise RuntimeError("TTS indisponible")

    _set_answer(monkeypatch, fake_answer)
    monkeypatch.setattr(main.speech, "text_to_voice_note", broken_tts)
    _post(client, _text_payload("Salam", "wamid.tts"))
    assert sent == [("texte", "Jërëjëf.")]


def test_agent_error_sends_spoken_apology(client, monkeypatch, sent):
    async def broken_answer(user, text):
        raise RuntimeError("quota")

    _set_answer(monkeypatch, broken_answer)
    _post(client, _text_payload("Salam", "wamid.2"))
    assert sent == [("vocal", "wolof:" + main.textes.ERREUR["wolof"])]


def test_location_message_sends_nearest_health_post(client, monkeypatch, sent):
    async def fake_answer(user, text, position=None):
        assert position == {"lat": 14.755, "lon": -17.39}
        agent._pending_actions[user] = [
            {"type": "location", "lat": 14.76, "lon": -17.39, "nom": "Poste", "adresse": ""}
        ]
        return "Poste bi mi gën a jege, 600 m la.", "wolof"

    agent = _set_answer(monkeypatch, fake_answer)
    loc = {"latitude": 14.755, "longitude": -17.39}
    _post(client, _media_payload("wamid.loc", "location", loc))
    assert sent == [("vocal", "wolof:Poste bi mi gën a jege, 600 m la."), ("carte", "Poste")]


def test_voice_question_gets_voice_then_location_button(client, monkeypatch, sent):
    async def fake_answer(user, text):
        agent._pending_actions[user] = [{"type": "location_request"}]
        return "Bësal bouton bi ci suuf.", "wolof"

    agent = _set_answer(monkeypatch, fake_answer)
    _post(client, _media_payload("wamid.voice1", "audio", {"id": "media.1"}))
    assert sent == [
        ("vocal", "wolof:Bësal bouton bi ci suuf."),
        ("bouton position", main.MSG_DEMANDE_POSITION),
    ]


def test_spoken_stop_unsubscribes(client, monkeypatch, sent):
    async def fake_transcribe(audio):
        return "Stop."

    async def no_answer(user, text):
        raise AssertionError("« stop » ne passe pas par l'agent")

    calls = []
    _set_answer(monkeypatch, no_answer)
    monkeypatch.setattr(main.speech, "transcribe", fake_transcribe)
    monkeypatch.setattr(main.onboarding.campaigns, "unsubscribe", calls.append)
    _post(client, _media_payload("wamid.stop", "audio", {"id": "media.2"}))
    assert calls == ["221700000000"]
    assert sent == [("vocal", "wolof:" + main.textes.DESINSCRIT["wolof"])]


def test_unreadable_voice_gets_spoken_message(client, monkeypatch, sent):
    async def silent(audio):
        return ""

    monkeypatch.setattr(main.speech, "transcribe", silent)
    _post(client, _media_payload("wamid.vide", "audio", {"id": "media.3"}))
    assert sent == [("vocal", "wolof:" + main.textes.VOCAL_KO["wolof"])]


def test_prescription_photo_explained_in_voice(client, monkeypatch, sent):
    from sunuchat import ordonnance

    async def fake_read(data, mime):
        assert mime == "image/jpeg"
        return ordonnance.Ordonnance(
            est_ordonnance=True,
            lisible=True,
            medicaments=[
                ordonnance.Medicament(
                    nom="Paracétamol", dosage="500 mg", posologie="1 comprimé 3 fois par jour"
                )
            ],
        )

    async def fake_answer(user, text, keep_language=False):
        assert keep_language and "Paracétamol : 500 mg, 1 comprimé 3 fois par jour" in text
        return "Paracétamol, benn comprimé ñetti yoon ci bés bi.", "pulaar"

    monkeypatch.setattr(main.ordonnance, "read", fake_read)
    _set_answer(monkeypatch, fake_answer)
    photo = {"id": "media.4", "mime_type": "image/jpeg"}
    _post(client, _media_payload("wamid.ordo", "image", photo))
    assert sent == [("vocal", "pulaar:Paracétamol, benn comprimé ñetti yoon ci bés bi.")]


def test_unreadable_prescription_gets_spoken_message(client, monkeypatch, sent):
    from sunuchat import ordonnance

    async def fake_read(data, mime):
        return ordonnance.Ordonnance(est_ordonnance=True, lisible=False)

    monkeypatch.setattr(main.ordonnance, "read", fake_read)
    _post(client, _media_payload("wamid.flou", "image", {"id": "media.5"}))
    assert sent == [("vocal", "wolof:" + main.textes.ORDONNANCE_ILLISIBLE["wolof"])]


def test_first_contact_subscribes_without_scripted_welcome(client, monkeypatch, sent):
    calls = []

    async def new_user(user):
        return {}

    async def fake_update(user, **values):
        calls.append(("profil", values))

    async def fake_answer(user, text):
        return "Dalal ak jàmm ! Maa ngi tudd SunuChat.", "wolof"

    monkeypatch.setattr(main, "_profile", new_user)
    monkeypatch.setattr(main.memory, "update_profile", fake_update)
    monkeypatch.setattr(main.campaigns, "subscribe", lambda u, langue: calls.append(langue))
    _set_answer(monkeypatch, fake_answer)
    _post(client, _text_payload("Salam", "wamid.hello"))
    assert calls == [("profil", {"accueilli": True}), "wolof"]
    assert sent == [("vocal", "wolof:Dalal ak jàmm ! Maa ngi tudd SunuChat.")]


def test_red_flag_sends_samu_contact(client, monkeypatch, sent):
    async def fake_answer(user, text):
        agent._pending_actions[user] = [{"type": "samu"}]
        return "Demal léegi lopitaal bi.", "wolof"

    agent = _set_answer(monkeypatch, fake_answer)
    _post(client, _text_payload("Sama doom dafay tiit", "wamid.samu"))
    assert sent == [("vocal", "wolof:Demal léegi lopitaal bi."), ("fiche", "1515")]


# Sérère : transcription en sérère, réponse lue par la voix pulaar, messages fixes en wolof.
def test_serer_voice_message(client, monkeypatch, sent):
    async def serer_user(user):
        return {"accueilli": True, "langue": "serer"}

    heard = []

    async def fake_transcribe(audio, language=None):
        heard.append(language)
        return "O fud-es ka kulin."

    async def fake_answer(user, text):
        return "Maa yega o ɗomel nu cer-of ?", "serer"

    monkeypatch.setattr(main, "_profile", serer_user)
    monkeypatch.setattr(main.speech, "transcribe", fake_transcribe)
    _set_answer(monkeypatch, fake_answer)
    _post(client, _media_payload("wamid.srr", "audio", {"id": "media.srr"}))
    assert heard == ["serer"]  # Kiriku transcrit en sérère
    assert sent == [("vocal", "serer:Maa yega o ɗomel nu cer-of ?")]


def test_serer_user_error_message_in_wolof(client, monkeypatch, sent):
    async def serer_user(user):
        return {"accueilli": True, "langue": "serer"}

    async def broken_answer(user, text):
        raise RuntimeError("quota")

    monkeypatch.setattr(main, "_profile", serer_user)
    _set_answer(monkeypatch, broken_answer)
    _post(client, _text_payload("Na fio xani ?", "wamid.srr2"))
    assert sent == [("vocal", "wolof:" + main.textes.ERREUR["wolof"])]
