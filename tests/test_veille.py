import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from sunuchat import campaigns, memory, speech, textes, veille, whatsapp
from sunuchat.config import get_settings

RSS = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
<item><title>Diphtérie : point de situation</title>
<link>https://www.sante.gouv.sn/Actualites/diphterie</link>
<pubDate>Mon, 12 Oct 2026 10:00:00 +0000</pubDate>
<description>&lt;p&gt;76 cas à &lt;b&gt;Kédougou&lt;/b&gt;&lt;/p&gt;</description></item>
<item><title>Vague de chaleur</title><link>https://www.anacim.sn/spip.php?article1</link>
<dc:date>2026-10-13T08:00:00Z</dc:date><description>Alerte canicule</description></item>
</channel></rss>"""


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Base vide, faux WhatsApp, fausse voix, fausse traduction, profils en mémoire."""
    s = get_settings()
    monkeypatch.setattr(s, "data_dir", tmp_path)
    monkeypatch.setattr(s, "template_alerte_sante", "")
    monkeypatch.setattr(s, "veille_test_numeros", "")
    monkeypatch.setattr(s, "veille_max_par_semaine", 3)
    veille._voice_cache.clear()
    sent = []
    profiles = {}

    async def fake_profile(user):
        return profiles.get(user, {"langue": "wolof"})

    async def fake_update(user, **values):
        profiles.setdefault(user, {}).update(values)

    async def fake_voice(text, langue):
        sent.append(("voix", langue, text))
        return b"ogg"

    async def fake_audio(to, ogg):
        sent.append(("audio", to))

    async def fake_text(to, text):
        sent.append(("texte", to, text))

    async def fake_template(to, name, language, params=None, image_id=None):
        sent.append(("modele", to, name, params))

    async def fake_traduire(texte_fr, langue):
        return f"[{langue}] {texte_fr}"

    async def no_region(place):
        return None

    from sunuchat import agent, places

    monkeypatch.setattr(memory, "get_profile", fake_profile)
    monkeypatch.setattr(memory, "update_profile", fake_update)
    monkeypatch.setattr(speech, "text_to_voice_note", fake_voice)
    monkeypatch.setattr(whatsapp, "send_audio", fake_audio)
    monkeypatch.setattr(whatsapp, "send_text", fake_text)
    monkeypatch.setattr(whatsapp, "send_template", fake_template)
    monkeypatch.setattr(agent, "traduire", fake_traduire)
    monkeypatch.setattr(places, "region_of", no_region)
    monkeypatch.setattr(veille.asyncio, "sleep", _no_sleep)
    return sent, profiles


async def _no_sleep(_):
    return None


def _alerte(**kw) -> dict:
    a = {
        "url": "https://www.sante.gouv.sn/Actualites/diphterie",
        "source": veille.MINISTERE,
        "titre": "Diphtérie",
        "date": "12/10/2026",
        "sujet": "Diphtérie",
        "regions": [],
        "message_fr": "Selon le Ministère de la Santé, des cas de diphtérie sont signalés.",
        "gravite": 2,
    }
    return {**a, **kw}


def test_rss_parsing_and_dates():
    items = veille._rss_items(RSS)
    assert items[0]["titre"] == "Diphtérie : point de situation"
    assert items[0]["texte"] == "76 cas à Kédougou"
    assert items[0]["date"] == datetime(2026, 10, 12, 10, tzinfo=UTC)
    assert items[1]["date"] == datetime(2026, 10, 13, 8, tzinfo=UTC)  # dc:date (SPIP)
    assert veille.date_fr("Lundi, décembre 15, 2025") == datetime(2025, 12, 15, tzinfo=UTC)
    assert veille.date_fr("1er août 2026") == datetime(2026, 8, 1, tzinfo=UTC)
    assert veille.date_fr("pas de date") is None


def test_regions_targeting():
    assert veille.norm("Région de Kédougou") == "kedougou"
    assert veille.concerned(["Kédougou"], "Region de Kedougou")
    assert not veille.concerned(["Kédougou"], "Dakar")
    assert veille.concerned([], "Dakar")  # alerte nationale
    assert veille.concerned(["Kédougou"], None)  # région inconnue : on prévient


def test_window_of_24_hours(monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    assert not campaigns.window_open("221")
    campaigns.subscribe("221", "wolof")  # s'inscrire, c'est écrire au bot
    assert campaigns.window_open("221")
    assert not campaigns.window_open("221", now=datetime.now() + timedelta(hours=25))
    campaigns.touch("221")
    assert campaigns.window_open("221")


def test_spoken_alert_always_cites_source_and_asks_to_verify(env):
    sent, _ = env
    asyncio.run(veille.send_alert("221", _alerte(), "pulaar"))
    voix, audio, note = sent
    assert voix[0] == "voix" and voix[1] == "pulaar"
    assert voix[2].startswith(textes.ALERTE_DEBUT["pulaar"])
    assert voix[2].endswith(textes.ALERTE_VERIFIER["pulaar"].format(source=veille.MINISTERE))
    assert audio == ("audio", "221")
    assert "Source : Ministère de la Santé, 12/10/2026" in note[2]
    assert "https://www.sante.gouv.sn/Actualites/diphterie" in note[2]
    assert "Vérifie" in note[2]


def test_diffuse_targets_region_window_and_template(env, monkeypatch):
    sent, profiles = env
    for numero in ("221dakar", "221kedougou", "221inconnu"):
        campaigns.subscribe(numero, "wolof")
    profiles["221dakar"] = {"langue": "wolof", "region": "Dakar"}
    profiles["221kedougou"] = {"langue": "pulaar", "region": "Kédougou"}
    # Fenêtre fermée pour la personne dont on ne connaît pas la région.
    with campaigns._db() as con:
        con.execute("UPDATE abonnes SET derniere_activite = '2020-01-01T00:00:00'")
    campaigns.touch("221kedougou")
    monkeypatch.setattr(get_settings(), "template_alerte_sante", "alerte_sante")

    touched = asyncio.run(veille.diffuse(_alerte(regions=["Kédougou"])))

    assert touched == 2
    destinataires = {x[1] for x in sent if x[0] in ("audio", "modele")}
    assert destinataires == {"221kedougou", "221inconnu"}  # pas Dakar
    assert ("modele", "221inconnu", "alerte_sante", ["Diphtérie", veille.MINISTERE]) in sent
    assert ("voix", "pulaar") == sent[0][:2]

    # Elle appuie sur « Écouter » : l'alerte en attente part en vocal, une seule fois.
    sent.clear()
    assert asyncio.run(veille.deliver_pending("221inconnu", "wolof")) == 1
    assert ("audio", "221inconnu") in sent
    assert asyncio.run(veille.deliver_pending("221inconnu", "wolof")) == 0


def test_weekly_cap_except_danger(env, monkeypatch):
    sent, _ = env
    campaigns.subscribe("221", "wolof")
    monkeypatch.setattr(get_settings(), "veille_max_par_semaine", 1)
    asyncio.run(veille.diffuse(_alerte(url="u1")))
    asyncio.run(veille.diffuse(_alerte(url="u2")))  # plafond atteint
    asyncio.run(veille.diffuse(_alerte(url="u3", gravite=3)))  # danger : passe quand même
    assert sum(1 for x in sent if x[0] == "audio") == 2


def test_test_numbers_bypass_subscribers(env, monkeypatch):
    sent, _ = env
    campaigns.subscribe("221abonne", "wolof")
    monkeypatch.setattr(get_settings(), "veille_test_numeros", "221test")
    asyncio.run(veille.diffuse(_alerte(regions=["Matam"])))
    assert {x[1] for x in sent if x[0] == "audio"} == {"221test"}


def _fake_sources(monkeypatch, pubs):
    async def collect(client):
        return [veille.Publication(**p) for p in pubs]

    monkeypatch.setattr(veille, "COLLECTORS", (collect,))


def _fake_analyse(monkeypatch, utile=True, gravite=2, sujet="Diphtérie"):
    calls = []

    async def analyse(pub):
        calls.append(pub.url)
        return veille.Analyse(
            utile=utile,
            gravite=gravite,
            sujet=sujet,
            regions=[],
            message_fr="Selon le Ministère de la Santé, des cas sont signalés.",
        )

    monkeypatch.setattr(veille, "analyse", analyse)
    return calls


def test_first_pass_is_silent_then_new_publications_are_sent(env, monkeypatch):
    sent, _ = env
    campaigns.subscribe("221", "wolof")
    now = datetime.now(UTC)
    old = {"source": "Ministère de la Santé", "titre": "Ancien", "url": "u-old", "date": now}
    _fake_sources(monkeypatch, [old])
    calls = _fake_analyse(monkeypatch)

    assert asyncio.run(veille.run_once()) == []  # premier passage : rien n'est envoyé
    assert calls == [] and sent == []

    new = {**old, "titre": "Nouveau", "url": "u-new", "complet": True}
    _fake_sources(monkeypatch, [old, new])
    results = asyncio.run(veille.run_once())
    assert calls == ["u-new"]
    assert results[0]["envoyes"] == 1
    assert veille.alertes_en_cours().startswith("- Diphtérie (tout le Sénégal)")

    # Même sujet, quelques heures plus tard : pas de deuxième alerte.
    again = {**new, "url": "u-again"}
    _fake_sources(monkeypatch, [old, new, again])
    results = asyncio.run(veille.run_once())
    assert "envoyes" not in results[0]
    assert sum(1 for x in sent if x[0] == "audio") == 1


def test_old_or_useless_publications_are_not_sent(env, monkeypatch):
    sent, _ = env
    campaigns.subscribe("221", "wolof")
    now = datetime.now(UTC)
    pubs = [
        {"source": "OMS", "titre": "Vieux", "url": "u1", "date": now - timedelta(days=30)},
        {"source": "OMS", "titre": "Nomination", "url": "u2", "date": now, "complet": True},
    ]
    _fake_sources(monkeypatch, pubs)
    calls = _fake_analyse(monkeypatch, utile=False)
    results = asyncio.run(veille.run_once(tout=True))
    assert calls == ["u2"]  # la publication trop ancienne n'est même pas analysée
    assert results[0]["utile"] is False
    assert sent == []


def test_dry_run_writes_and_sends_nothing(env, monkeypatch):
    sent, _ = env
    campaigns.subscribe("221", "wolof")
    pub = {"source": "OMS", "titre": "Dengue", "url": "u1", "date": datetime.now(UTC)}
    _fake_sources(monkeypatch, [{**pub, "complet": True}])
    _fake_analyse(monkeypatch)
    results = asyncio.run(veille.run_once(dry_run=True, tout=True))
    assert results[0]["sujet"] == "Diphtérie" and "envoyes" not in results[0]
    assert sent == []
    with veille._db() as con:
        assert con.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0


def test_broken_source_does_not_stop_the_others(env, monkeypatch):
    async def broken(client):
        raise RuntimeError("site en panne")

    async def ok(client):
        return [veille.Publication("OMS", "Dengue", "u1")]

    monkeypatch.setattr(veille, "COLLECTORS", (broken, ok))
    pubs = asyncio.run(veille.collect_all(None))
    assert [p.url for p in pubs] == ["u1"]


def test_advice_is_sent_and_announced_as_advice(env, monkeypatch):
    sent, _ = env
    campaigns.subscribe("221", "wolof")
    pub = {"source": "APS", "titre": "Octobre rose", "url": "u1", "complet": True}
    _fake_sources(monkeypatch, [{**pub, "date": datetime.now(UTC)}])

    async def analyse(p):
        return veille.Analyse(
            utile=True,
            type="conseil",
            gravite=1,
            sujet="Dépistage du cancer du sein",
            message_fr="Selon l'Agence de presse sénégalaise, le dépistage est gratuit.",
        )

    monkeypatch.setattr(veille, "analyse", analyse)
    results = asyncio.run(veille.run_once(tout=True))
    assert results[0]["envoyes"] == 1  # un conseil (gravité 1) part aussi
    voix = next(x for x in sent if x[0] == "voix")
    assert voix[2].startswith(textes.CONSEIL_DEBUT["wolof"])
    note = next(x for x in sent if x[0] == "texte")
    assert note[2].startswith("💡 Info santé SunuChat : Dépistage du cancer du sein")


def test_test_mode_skips_silent_first_pass(env, monkeypatch):
    sent, _ = env
    monkeypatch.setattr(get_settings(), "veille_test_numeros", "221test")
    pub = {"source": "APS", "titre": "Vaccination", "url": "u1", "complet": True}
    _fake_sources(monkeypatch, [{**pub, "date": datetime.now(UTC)}])
    _fake_analyse(monkeypatch)
    results = asyncio.run(veille.run_once())
    assert results[0]["envoyes"] == 1
    assert {x[1] for x in sent if x[0] == "audio"} == {"221test"}
    # Au passage suivant, la publication est déjà vue : pas de doublon.
    assert asyncio.run(veille.run_once()) == []


def test_rss_categories_for_le_soleil():
    xml = """<rss><channel>
    <item><title>Don de sang</title><link>https://lesoleil.sn/actualites/sante/don</link>
    <category><![CDATA[Santé]]></category></item>
    <item><title>Match</title><link>https://lesoleil.sn/sport/match</link>
    <category><![CDATA[Sports]]></category></item>
    </channel></rss>"""
    items = veille._rss_items(xml)
    assert items[0]["categories"] == ["Santé"]
    assert [veille.norm(c) for c in items[0]["categories"]] == ["sante"]


def test_old_database_gets_type_column(monkeypatch, tmp_path):
    import sqlite3

    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    con = sqlite3.connect(tmp_path / "veille.db")
    con.execute(
        "CREATE TABLE publications (url TEXT PRIMARY KEY, source TEXT, titre TEXT, "
        "publie_le TEXT, vue_le TEXT, utile INTEGER, gravite INTEGER, sujet TEXT, "
        "regions TEXT, message_fr TEXT, diffusee_le TEXT)"
    )
    con.execute("INSERT INTO publications (url, sujet) VALUES ('u1', 'FVR')")
    con.commit()
    con.close()
    with veille._db() as con:
        assert con.execute("SELECT type FROM publications").fetchone() == ("alerte",)
