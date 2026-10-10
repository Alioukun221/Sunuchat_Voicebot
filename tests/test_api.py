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


def test_post_text_message_answers(client, monkeypatch):
    sent = []

    async def fake_send_text(to, text):
        sent.append((to, text))

    async def fake_typing(message_id):
        return None

    async def fake_answer(user, text):
        return f"réponse à {text}", "wolof"

    from sunuchat import agent

    monkeypatch.setattr(main.whatsapp, "send_text", fake_send_text)
    monkeypatch.setattr(main.whatsapp, "mark_read_and_typing", fake_typing)
    monkeypatch.setattr(agent, "answer", fake_answer)
    body, headers = _signed(_text_payload("Salam"))
    assert client.post("/webhook", content=body, headers=headers).status_code == 200
    # Le même message renvoyé par Meta n'est traité qu'une fois.
    client.post("/webhook", content=body, headers=headers)
    assert sent == [("221700000000", "réponse à Salam")]


def test_agent_error_sends_friendly_message(client, monkeypatch):
    sent = []

    async def fake_send_text(to, text):
        sent.append(text)

    async def fake_typing(message_id):
        return None

    async def broken_answer(user, text):
        raise RuntimeError("quota")

    from sunuchat import agent

    monkeypatch.setattr(main.whatsapp, "send_text", fake_send_text)
    monkeypatch.setattr(main.whatsapp, "mark_read_and_typing", fake_typing)
    monkeypatch.setattr(agent, "answer", broken_answer)
    body, headers = _signed(_text_payload("Salam", "wamid.2"))
    client.post("/webhook", content=body, headers=headers)
    assert sent == [main.MSG_ERREUR]


def test_location_message_sends_nearest_health_post(client, monkeypatch):
    sent = []

    async def fake_send_text(to, text):
        sent.append(("text", text))

    async def fake_send_location(to, lat, lon, name="", address=""):
        sent.append(("location", name))

    async def fake_typing(message_id):
        return None

    from sunuchat import agent

    async def fake_answer(user, text, position=None):
        assert position == {"lat": 14.755, "lon": -17.39}
        agent._pending_actions[user] = [
            {
                "type": "location",
                "lat": 14.76,
                "lon": -17.39,
                "nom": "Poste de Santé",
                "adresse": "",
            }
        ]
        return "Le poste le plus proche est à 600 mètres.", "wolof"

    monkeypatch.setattr(main.whatsapp, "send_text", fake_send_text)
    monkeypatch.setattr(main.whatsapp, "send_location", fake_send_location)
    monkeypatch.setattr(main.whatsapp, "mark_read_and_typing", fake_typing)
    monkeypatch.setattr(agent, "answer", fake_answer)
    payload = _text_payload("", "wamid.loc")
    msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.pop("text")
    msg.update(type="location", location={"latitude": 14.755, "longitude": -17.39})
    body, headers = _signed(payload)
    client.post("/webhook", content=body, headers=headers)
    assert sent == [
        ("text", "Le poste le plus proche est à 600 mètres."),
        ("location", "Poste de Santé"),
    ]


def _audio_payload(msg_id):
    payload = _text_payload("", msg_id)
    msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.pop("text")
    msg.update(type="audio", audio={"id": "media.1"})
    return payload


def _voice_fakes(monkeypatch, sent, tts_ok=True):
    from sunuchat import agent

    async def fake_send_text(to, text):
        sent.append(("text", text))

    async def fake_send_audio(to, ogg):
        sent.append(("audio", ogg))

    async def fake_send_location_request(to, text):
        sent.append(("bouton", text))

    async def fake_typing(message_id):
        return None

    async def fake_download(media_id):
        return b"ogg"

    async def fake_transcribe(audio):
        return "Fan la poste de santé bi gën a jege nekk ?"

    async def fake_tts(text, langue):
        if not tts_ok:
            raise RuntimeError("TTS indisponible")
        return b"voix"

    async def fake_answer(user, text, position=None):
        agent._pending_actions[user] = [{"type": "location_request"}]
        return "Bësal bouton bi ngir yónnee sa position.", "wolof"

    monkeypatch.setattr(main.whatsapp, "send_text", fake_send_text)
    monkeypatch.setattr(main.whatsapp, "send_audio", fake_send_audio)
    monkeypatch.setattr(main.whatsapp, "send_location_request", fake_send_location_request)
    monkeypatch.setattr(main.whatsapp, "mark_read_and_typing", fake_typing)
    monkeypatch.setattr(main.whatsapp, "download_media", fake_download)
    monkeypatch.setattr(main.speech, "transcribe", fake_transcribe)
    monkeypatch.setattr(main.speech, "text_to_voice_note", fake_tts)
    monkeypatch.setattr(agent, "answer", fake_answer)


def test_voice_message_gets_voice_only_then_button(client, monkeypatch):
    sent = []
    _voice_fakes(monkeypatch, sent)
    body, headers = _signed(_audio_payload("wamid.voice1"))
    client.post("/webhook", content=body, headers=headers)
    assert sent == [("audio", b"voix"), ("bouton", main.MSG_DEMANDE_POSITION)]


def test_voice_falls_back_to_text_when_tts_fails(client, monkeypatch):
    sent = []
    _voice_fakes(monkeypatch, sent, tts_ok=False)
    body, headers = _signed(_audio_payload("wamid.voice2"))
    client.post("/webhook", content=body, headers=headers)
    assert sent[0] == ("text", "Bësal bouton bi ngir yónnee sa position.")
    assert ("audio", b"voix") not in sent


def test_location_after_voice_answers_in_voice(client, monkeypatch):
    sent = []
    _voice_fakes(monkeypatch, sent)
    body, headers = _signed(_audio_payload("wamid.voice3"))
    client.post("/webhook", content=body, headers=headers)
    sent.clear()
    payload = _text_payload("", "wamid.voice3.loc")
    msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.pop("text")
    msg.update(type="location", location={"latitude": 14.7, "longitude": -17.4})
    body, headers = _signed(payload)
    client.post("/webhook", content=body, headers=headers)
    assert sent[0] == ("audio", b"voix")
    assert not any(kind == "text" for kind, _ in sent)


def _interactive_payload(reply_id, msg_id):
    payload = _text_payload("", msg_id)
    msg = payload["entry"][0]["changes"][0]["value"]["messages"][0]
    msg.pop("text")
    kind = "button_reply" if reply_id.startswith("langue_") else "list_reply"
    msg.update(type="interactive", interactive={"type": kind, kind: {"id": reply_id}})
    return payload


def test_first_greeting_gets_language_card_only(client, monkeypatch):
    calls = []

    async def new_user(user):
        return {}

    async def fake_update(user, **values):
        calls.append(("profil", values))

    async def fake_card(user):
        calls.append(("carte", user))

    async def fake_typing(message_id):
        return None

    async def no_answer(user, text, **kw):
        raise AssertionError("pas de réponse de l'agent à une simple salutation")

    from sunuchat import agent

    monkeypatch.setattr(main, "_profile", new_user)
    monkeypatch.setattr(main.memory, "update_profile", fake_update)
    monkeypatch.setattr(main.onboarding, "send_language_card", fake_card)
    monkeypatch.setattr(main.whatsapp, "mark_read_and_typing", fake_typing)
    monkeypatch.setattr(agent, "answer", no_answer)
    body, headers = _signed(_text_payload("Salam", "wamid.hello"))
    client.post("/webhook", content=body, headers=headers)
    assert calls == [("profil", {"accueilli": True}), ("carte", "221700000000")]


def test_language_button_and_menu_choice(client, monkeypatch):
    sent = []

    async def fake_choose(user, langue):
        sent.append(("langue", langue))

    async def fake_typing(message_id):
        return None

    async def fake_answer(user, text, **kw):
        sent.append(("question", text, kw))
        return "Sibiru dafay indi feebar.", "wolof"

    async def fake_image(user, name, caption=""):
        sent.append(("image", name))

    async def fake_tts(text, langue):
        return b"voix"

    async def fake_audio(to, ogg):
        sent.append(("audio", ogg))

    from sunuchat import agent

    monkeypatch.setattr(main.onboarding, "choose_language", fake_choose)
    monkeypatch.setattr(main.whatsapp, "mark_read_and_typing", fake_typing)
    monkeypatch.setattr(main.images, "send", fake_image)
    monkeypatch.setattr(main.speech, "text_to_voice_note", fake_tts)
    monkeypatch.setattr(main.whatsapp, "send_audio", fake_audio)
    monkeypatch.setattr(agent, "answer", fake_answer)
    body, headers = _signed(_interactive_payload("langue_pulaar", "wamid.b1"))
    client.post("/webhook", content=body, headers=headers)
    body, headers = _signed(_interactive_payload("menu_palu", "wamid.b2"))
    client.post("/webhook", content=body, headers=headers)
    assert sent == [
        ("langue", "pulaar"),
        ("question", main.textes.QUESTIONS_MENU["menu_palu"], {"language": "wolof"}),
        ("image", "paludisme"),
        ("audio", b"voix"),
    ]


def test_red_flag_sends_samu_contact(client, monkeypatch):
    sent = []

    async def fake_send_text(to, text):
        sent.append(("text", text))

    async def fake_contact(to, name, phone):
        sent.append(("contact", phone))

    async def fake_typing(message_id):
        return None

    from sunuchat import agent

    async def fake_answer(user, text, **kw):
        agent._pending_actions[user] = [{"type": "samu"}]
        return "Va tout de suite à l'hôpital.", "francais"

    monkeypatch.setattr(main.whatsapp, "send_text", fake_send_text)
    monkeypatch.setattr(main.whatsapp, "send_contact", fake_contact)
    monkeypatch.setattr(main.whatsapp, "mark_read_and_typing", fake_typing)
    monkeypatch.setattr(agent, "answer", fake_answer)
    body, headers = _signed(_text_payload("Mon fils convulse", "wamid.samu"))
    client.post("/webhook", content=body, headers=headers)
    assert sent == [("text", "Va tout de suite à l'hôpital."), ("contact", "1515")]
