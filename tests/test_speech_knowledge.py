import array
import asyncio
import io
import wave

import httpx

from sunuchat import knowledge, speech

RATE = 22050


def _wav(*parts: tuple[float, bool]) -> bytes:
    """WAV fait de morceaux (durée en s, son ou silence)."""
    samples = array.array("h")
    for seconds, loud in parts:
        n = int(seconds * RATE)
        samples.extend([8000 if i % 2 else -8000 for i in range(n)] if loud else [0] * n)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.tobytes())
    return buf.getvalue()


def _duration(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return w.getnframes() / w.getframerate()


def test_split_text_respects_limit():
    text = " ".join(["Bul fàtte di tëdd ci biir moustiquaire bi."] * 40)
    chunks = speech.split_text(text, limit=500)
    assert len(chunks) > 1
    assert all(len(c) <= 500 for c in chunks)


def test_concat_wavs_trims_long_silences():
    first = _wav((0.5, False), (1.0, True), (2.0, False), (1.0, True), (0.8, False))
    second = _wav((0.4, False), (0.5, True), (1.0, False))
    # Un seul audio : silences du début et de la fin coupés, pause de 2 s ramenée à 0,8 s.
    assert abs(_duration(speech.concat_wavs([first])) - 2.8) < 0.05
    # Réponse trop longue pour une requête : 2,8 + 0,3 entre les morceaux + 0,5
    assert abs(_duration(speech.concat_wavs([first, second])) - 3.6) < 0.05


def test_pronunciation_and_dashes():
    speech.PRONUNCIATION["wolof"]["sibiru"] = "sibbiru"
    try:
        text = speech.clean_for_speech("Sibiru bi — dafa metti (xale yi).", "wolof")
    finally:
        del speech.PRONUNCIATION["wolof"]["sibiru"]
    assert text == "sibbiru bi, dafa metti, xale yi."


def test_clean_for_speech():
    text = (
        "**Attention** 🦟 :\n- Dormez sous une moustiquaire...\n"
        "- Consultez vite (https://pnlp.sn)\n\nFièvre à 38°C ? Allez au poste de santé !"
    )
    assert speech.clean_for_speech(text) == (
        "Attention: Dormez sous une moustiquaire. Consultez vite. "
        "Fièvre à trente-huit degrés? Allez au poste de santé!"
    )


def test_synthesize_sends_voice_params(monkeypatch):
    sent = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            sent.append(json)
            return httpx.Response(
                200, content=_wav((0.5, True)), request=httpx.Request("POST", url)
            )

    monkeypatch.setattr(speech, "_client", FakeClient)
    asyncio.run(speech.synthesize("Bul fàtte moustiquaire bi. Jërëjëf ! Na nga def ?", "pulaar"))
    # Une seule requête pour toute la réponse : la ponctuation fait les pauses.
    assert len(sent) == 1
    assert sent[0]["input"] == "Bul fàtte moustiquaire bi. Jërëjëf! Na nga def?"
    assert sent[0]["speed"] == 0.8 and sent[0]["pitch"] == 0.0


def test_clean_pages_removes_headers_and_page_numbers():
    pages = [f"PNLP Sénégal\nLa moustiquaire protège la famille {i}.\n{i}" for i in range(1, 6)]
    cleaned = knowledge.clean_pages(pages)
    assert all("PNLP Sénégal" not in p for p in cleaned)
    assert cleaned[0] == "La moustiquaire protège la famille 1."


def test_chunk_text_overlaps():
    text = "Phrase sur le paludisme. " * 200
    chunks = knowledge.chunk_text(text)
    assert len(chunks) > 1
    assert all(len(c) <= knowledge.CHUNK_CHARS for c in chunks)


def test_spell_numbers():
    text = speech.clean_for_speech("Appelle le SAMU au 1515. Fièvre à 38°C, 2 fois.")
    assert text == "Appelle le SAMU au quinze quinze. Fièvre à trente-huit degrés, deux fois."
    assert speech.spell_numbers("800 00 50 50") == "huit cents zéro zéro cinquante cinquante"
    assert (
        speech.spell_numbers("33 869 07 07")
        == "trente-trois huit cent soixante-neuf zéro sept zéro sept"
    )
    assert speech.spell_numbers("1 200 enfants") == "mille deux cents enfants"
    assert speech.spell_numbers("12 000 cas") == "douze mille cas"
    assert speech.spell_numbers("à 0,6 km") == "à zéro virgule six kilomètre"
    assert speech.spell_numbers("37.5°C") == "trente-sept virgule cinq degrés"
    assert speech.spell_numbers("0,05") == "zéro virgule zéro cinq"
    assert speech.spell_numbers("à 600 m, puis 2,3 km") == (
        "à six cents mètres, puis deux virgule trois kilomètres"
    )
    assert speech.spell_numbers("1, 2, 3") == "un, deux, trois"
    assert speech.number_to_words(2024) == "deux mille vingt-quatre"
    assert speech.number_to_words(71) == "soixante et onze"
    assert speech.number_to_words(80) == "quatre-vingts"
    assert speech.number_to_words(91) == "quatre-vingt-onze"
    assert speech.phone_to_words("77123") == "sept soixante et onze vingt-trois"
