import array
import asyncio
import io
import wave
from types import SimpleNamespace

import httpx

from sunuchat import agent, onboarding, serer, speech, textes

FIEVRE_FR = "Ton enfant a de la fièvre. Donne-lui de l'eau et va au poste de santé."


def _wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(22050)
        w.writeframes(array.array("h", [8000, -8000] * 2000).tobytes())
    return buf.getvalue()


# ---------- Lexique et phrases ----------


def test_lexicon_entries_are_sourced():
    entries = serer.lexicon()
    assert len(entries) > 400
    for e in entries:
        assert e.francais, e
        assert e.page.isdigit() and 1 <= int(e.page) <= 104, e  # pages du lexique de Crétois


def test_phrases_are_well_formed():
    ids = [p.id for p in serer.phrases()]
    assert len(ids) == len(set(ids))
    for p in serer.phrases():
        assert p.statut in ("atteste", "valide", "a_traduire"), p
        # Une phrase attestée ou validée a sa traduction ; une phrase à traduire n'en a pas.
        assert bool(p.sereer) == (p.statut != "a_traduire"), p
    assert len(serer.validated_phrases()) >= 20


def test_glossary_finds_health_words():
    found = {e.sereer for e in serer.glossary("L'enfant a de la fièvre et mal au ventre.")}
    assert {"pokan", "fud"} <= found


def test_gloss_explains_serer_words():
    gloss = serer.gloss("O fud-es ka kulin. Moyana-xam.")
    assert "- fud : le ventre" in gloss
    assert "- kulin : avoir le ventre gonflé" in gloss
    assert "- moyana : aller mieux" in gloss  # forme fléchie de « moyan »


# ---------- Vérification d'une réponse sérère ----------


def test_check_accepts_dictionary_words_and_french_loanwords():
    verdict = serer.check("Na fio xani ? O njay a ɓook. Reti poste de santé.", FIEVRE_FR)
    assert verdict.ok and verdict.unknown == []


def test_check_rejects_invented_words():
    verdict = serer.check("O njay a ɓook. Bakatu ndiyoolo mbarafi.", FIEVRE_FR)
    assert not verdict.ok
    assert verdict.unknown == ["bakatu", "ndiyoolo", "mbarafi"]


def test_check_rejects_french_in_disguise():
    # Mots tous connus (repris du français), mais presque aucun mot sérère.
    verdict = serer.check("Ton enfant a de la fièvre, donne-lui de l'eau.", FIEVRE_FR)
    assert not verdict.ok and verdict.serer_share < serer.MIN_SERER


def test_check_rejects_wolof():
    assert not serer.check("Sa doom dafa am yaram bu tàng.", FIEVRE_FR).ok


# ---------- Voix ----------


def test_voice_letters_and_params(monkeypatch):
    sent = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            sent.append(json)
            return httpx.Response(200, content=_wav(), request=httpx.Request("POST", url))

    monkeypatch.setattr(speech, "_client", FakeClient)
    asyncio.run(speech.synthesize("Xa damong ? Yoqi fa jam, 2 fois.", "serer"))
    # Voix et réglages du pulaar ; x et q, que la voix n'a pas appris, deviennent h et k.
    assert sent[0]["voice"] == "pulaar"
    assert {k: sent[0][k] for k in ("speed", "pitch")} == speech.VOICE_PARAMS["pulaar"]
    assert sent[0]["input"] == "ha damong? Yoki fa jam, deux fois."


# ---------- Textes fixes et changement de langue ----------


def test_fixed_texts_fall_back_to_wolof():
    assert textes.texte(textes.ERREUR, "serer") == (textes.ERREUR["wolof"], "wolof")
    assert textes.texte(textes.ERREUR, "pulaar") == (textes.ERREUR["pulaar"], "pulaar")
    assert textes.texte(textes.LANGUE_CHOISIE, "serer") == ("Na fio xani ?", "serer")


def test_language_commands(monkeypatch):
    assert onboarding.command("Seereer.") == "langue:serer"
    assert onboarding.command("sérère") == "langue:serer"
    assert onboarding.command("Wolof") == "langue:wolof"
    assert onboarding.language_of({"langue": "serer"}) == "serer"

    saved, said = [], []

    async def fake_update(user, **values):
        saved.append(values)

    async def fake_say(user, text, langue):
        said.append((text, langue))

    monkeypatch.setattr(onboarding.memory, "update_profile", fake_update)
    monkeypatch.setattr(onboarding, "say", fake_say)
    asyncio.run(onboarding.run_command("221", "langue:serer", "wolof"))
    assert saved == [{"langue": "serer"}]
    assert said == [("Na fio xani ?", "serer")]


# ---------- Agent ----------


def _ctx(**state):
    return SimpleNamespace(state=state)


def test_serer_is_kept_until_the_user_asks(monkeypatch):
    async def no_rag(texte, k=None):
        return []

    monkeypatch.setattr(agent.knowledge, "asearch", no_rag)
    ctx = _ctx(**{"user:langue": "serer", "comprehension": {"langue": "wolof", "texte_fr": "x"}})
    asyncio.run(agent.after_comprehension(ctx))
    assert ctx.state["user:langue"] == "serer"  # Gemini croit entendre du wolof : on reste
    ctx = _ctx(**{"user:langue": "wolof", "comprehension": {"langue": "serer", "texte_fr": "x"}})
    asyncio.run(agent.after_comprehension(ctx))
    assert ctx.state["user:langue"] == "serer"  # la personne a demandé le sérère


def test_comprehension_gets_serer_gloss():
    ctx = _ctx(**{"user:langue": "serer", "glossaire_sereer": "- fud : le ventre"})
    prompt = agent.comprehension_instruction(ctx)
    assert "- fud : le ventre" in prompt and "sérère" in prompt
    assert "- fud" not in agent.comprehension_instruction(_ctx(**{"user:langue": "wolof"}))


def test_serer_translation_prompt_uses_dictionary():
    prompt = agent.traduction_prompt("serer", FIEVRE_FR)
    assert "Na fio xani ?" in prompt  # phrases attestées
    assert "pokan" in prompt  # glossaire de la réponse (fièvre)
    assert FIEVRE_FR in prompt


def _meaning(fidele: bool, manque: list[str] | None = None):
    async def fake_check_meaning(reponse_fr, texte):
        return agent.Fidelite(
            retraduction="L'enfant a la fièvre.", fidele=fidele, consignes_manquantes=manque or []
        )

    return fake_check_meaning


def test_accepted_serer_reply(monkeypatch, tmp_path):
    monkeypatch.setattr(agent.get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(agent, "check_meaning", _meaning(True))

    async def fake_translate(texte_fr, langue):
        return "Sa doom am na yaram."

    monkeypatch.setattr(agent, "translate", fake_translate)
    ctx = _ctx(
        **{
            "user:langue": "serer",
            "reponse_fr": FIEVRE_FR,
            "reponse_finale": "O njay a ɓook. Reti poste de santé.",
        }
    )
    asyncio.run(agent.after_traduction(ctx))
    assert ctx.state["langue_reponse"] == "serer"
    assert (tmp_path / "serer_relecture.jsonl").exists()  # gardé pour la relecture


def test_rejected_serer_reply_goes_out_in_wolof(monkeypatch, tmp_path):
    monkeypatch.setattr(agent.get_settings(), "data_dir", tmp_path)

    async def fake_translate(texte_fr, langue):
        assert (texte_fr, langue) == (FIEVRE_FR, "wolof")
        return "Sa doom am na yaram."

    monkeypatch.setattr(agent, "translate", fake_translate)
    ctx = _ctx(
        **{
            "user:langue": "serer",
            "reponse_fr": FIEVRE_FR,
            "reponse_finale": "O njay a ɓook. Bakatu ndiyoolo mbarafi.",
        }
    )
    asyncio.run(agent.after_traduction(ctx))
    assert ctx.state["reponse_finale"] == "Sa doom am na yaram."
    assert ctx.state["langue_reponse"] == "wolof"


def test_serer_reply_that_loses_advice_goes_out_in_wolof(monkeypatch, tmp_path):
    # Tous les mots sont dans le dictionnaire, mais la retraduction a perdu un conseil.
    monkeypatch.setattr(agent.get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(agent, "check_meaning", _meaning(False, ["donner de l'eau"]))

    async def fake_translate(texte_fr, langue):
        return "Sa doom am na yaram."

    monkeypatch.setattr(agent, "translate", fake_translate)
    ctx = _ctx(
        **{
            "user:langue": "serer",
            "reponse_fr": FIEVRE_FR,
            "reponse_finale": "O njay a ɓook. Reti poste de santé.",
        }
    )
    asyncio.run(agent.after_traduction(ctx))
    assert ctx.state["langue_reponse"] == "wolof"
    log = (tmp_path / "serer_relecture.jsonl").read_text(encoding="utf-8")
    assert "donner de l'eau" in log and '"accepte": false' in log


def test_urgency_never_in_unreviewed_serer():
    state = {"user:langue": "serer", "alertes": ["convulsions"], "reponse_fr": "Va vite."}
    assert agent.translation_language(state) == "wolof"
    assert "wolof" in agent.traduction_instruction(_ctx(**state))
    assert agent.translation_language({**state, "alertes": []}) == "serer"
