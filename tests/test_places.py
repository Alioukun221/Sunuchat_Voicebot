import asyncio
from types import SimpleNamespace

from sunuchat import agent, places

# Autour de Pikine (Dakar)
LAT, LON = 14.7550, -17.3900

OVERPASS = {
    "elements": [
        {
            "type": "node",
            "lat": 14.7600,
            "lon": -17.3900,
            "tags": {
                "amenity": "clinic",
                "healthcare": "health_post",
                "name": "Poste de Santé de Thiaroye",
                "phone": "+221 33 000 00 00",
            },
        },
        {
            "type": "way",
            "center": {"lat": 14.7700, "lon": -17.3950},
            "tags": {"amenity": "hospital", "name": "Hôpital de Pikine"},
        },
        # Même poste en bâtiment, un peu plus loin : doublon à écarter.
        {
            "type": "way",
            "center": {"lat": 14.7610, "lon": -17.3905},
            "tags": {"amenity": "clinic", "name": "Poste de Santé de Thiaroye"},
        },
        {"type": "node", "lat": 14.7560, "lon": -17.3901, "tags": {"amenity": "clinic"}},
    ]
}


def test_distance_km():
    # Dakar → Thiès : environ 58 km à vol d'oiseau
    assert 55 < places.distance_km(14.6928, -17.4467, 14.7910, -16.9359) < 60


def test_parse_facilities_sorted_named_deduplicated():
    found = places.parse_facilities(OVERPASS, LAT, LON)
    assert [f["nom"] for f in found] == ["Poste de Santé de Thiaroye", "Hôpital de Pikine"]
    first = found[0]
    assert first["type"] == "poste de santé"
    assert first["distance_km"] == 0.6
    assert first["distance"] == "560 m"
    assert first["telephone"] == "+221 33 000 00 00"
    assert first["carte"].startswith("https://www.google.com/maps/search/?api=1&query=14.76")


def _ctx(state):
    return SimpleNamespace(state=state, user_id="221700000000")


def test_postes_tool_uses_gps_and_queues_map(monkeypatch):
    async def fake_nearby(lat, lon, limit=3):
        assert (lat, lon) == (LAT, LON)
        return places.parse_facilities(OVERPASS, lat, lon)

    monkeypatch.setattr(places, "nearby_health_facilities", fake_nearby)
    agent.pop_actions("221700000000")
    result = asyncio.run(
        agent.postes_de_sante_proches(_ctx({"user:position": {"lat": LAT, "lon": LON}}))
    )
    assert result["structures"][0]["nom"] == "Poste de Santé de Thiaroye"
    actions = agent.pop_actions("221700000000")
    assert actions[0]["type"] == "location" and actions[0]["nom"] == "Poste de Santé de Thiaroye"


def test_postes_tool_without_position_asks_for_it(monkeypatch):
    result = asyncio.run(agent.postes_de_sante_proches(_ctx({})))
    assert "demander_position" in result["erreur"]
    agent.demander_position(_ctx({}))
    assert agent.pop_actions("221700000000") == [{"type": "location_request"}]


def test_postes_tool_geocodes_localite(monkeypatch):
    async def fake_geocode(place):
        assert place == "Podor"
        return (16.65, -14.96)

    async def fake_nearby(lat, lon, limit=3):
        return []

    monkeypatch.setattr(places, "geocode", fake_geocode)
    monkeypatch.setattr(places, "nearby_health_facilities", fake_nearby)
    result = asyncio.run(agent.postes_de_sante_proches(_ctx({}), localite="Podor"))
    assert "aucune structure" in result["erreur"]


def test_build_app_has_new_tools():
    app = agent.build_app()
    sante = app.root_agent.sub_agents[1]
    names = {getattr(t, "name", getattr(t, "__name__", "")) for t in sante.tools}
    assert {"postes_de_sante_proches", "demander_position", "recherche_web"} <= names


def test_asks_for_facility():
    assert agent.asks_for_facility("Où est le poste de santé le plus proche ?")
    assert agent.asks_for_facility("Je cherche un hôpital près de chez moi")
    assert not agent.asks_for_facility("Comment entretenir la moustiquaire ?")


def test_distance_text():
    assert places.distance_text(0.62) == "620 m"
    assert places.distance_text(0.01) == "50 m"
    assert places.distance_text(2.34) == "2,3 km"
    assert places.distance_text(5.0) == "5 km"


def test_wolof_translation_prompt_uses_official_spelling():
    from types import SimpleNamespace

    from sunuchat import agent

    ctx = SimpleNamespace(state={"user:langue": "wolof", "reponse_fr": "Bonjour."})
    prompt = agent.traduction_instruction(ctx)
    assert "CLAD" in prompt and "Jërëjëf" in prompt and "Bonjour." in prompt


def test_translation_fallback_raises_when_everything_fails(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    import pytest

    from sunuchat import agent

    class Broken:
        def __init__(self, *a, **k):
            raise RuntimeError("pas de réseau")

    monkeypatch.setattr(agent.genai, "Client", Broken)
    ctx = SimpleNamespace(state={"reponse_fr": "Va au poste de santé.", "user:langue": "wolof"})
    with pytest.raises(RuntimeError):
        asyncio.run(agent.translation_fallback(ctx, None, RuntimeError("429 quota")))


def test_translation_fallback_retranslates_with_fast_model(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from google.genai import types

    from sunuchat import agent

    calls = []

    class Models:
        async def generate_content(self, model, contents, config):
            calls.append((model, contents))
            return types.GenerateContentResponse(
                candidates=[
                    types.Candidate(
                        content=types.Content(role="model", parts=[types.Part(text="Dem ci")])
                    )
                ]
            )

    class Client:
        aio = SimpleNamespace(models=Models())

    monkeypatch.setattr(agent.genai, "Client", Client)
    ctx = SimpleNamespace(state={"reponse_fr": "Va au poste de santé.", "user:langue": "wolof"})
    r = asyncio.run(agent.translation_fallback(ctx, None, RuntimeError("404")))
    assert r.content.parts[0].text == "Dem ci"
    assert "Va au poste de santé." in calls[0][1]
