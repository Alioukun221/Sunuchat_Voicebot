"""Voix wolof / pulaar via l'API Kiriku : transcription (STT) et synthèse vocale (TTS).

Kiriku renvoie du WAV, que WhatsApp refuse : on le convertit en OGG/Opus avec le
ffmpeg fourni par le paquet imageio-ffmpeg (rien à installer en plus).
"""

import array
import asyncio
import io
import logging
import re
import sys
import unicodedata
import wave

import httpx
import imageio_ffmpeg

from sunuchat.config import get_settings

log = logging.getLogger(__name__)

TTS_MODEL = "kiriku-tts"
STT_MODEL = "m-kiriku-asr"
TTS_VOICES = {"wolof", "pulaar"}  # pas de voix française
MAX_TTS_CHARS = 500  # l'API accepte 512 caractères par requête
# Réglages de voix validés à l'écoute.
VOICE_PARAMS = {
    "wolof": {"speed": 1.1, "pitch": 0.0},
    "pulaar": {"speed": 0.8, "pitch": 0.0},
}
# Pauses : la réponse est lue d'un seul tenant (une seule requête), et ce sont les signes
# de ponctuation (virgule, point, point d'interrogation…) qui font respirer la voix. On ne
# coupe que le silence du début et de la fin, et les silences anormaux de plus de
# MAX_PAUSE_S secondes.
SILENCE_THRESHOLD = 500  # amplitude max (PCM 16 bits) considérée comme du silence
MAX_PAUSE_S = 0.8

# Prononciation : mots que la voix lit mal, réécrits comme ils se disent. À compléter à
# l'écoute (clé : mot tel qu'écrit par Gemini, sans tenir compte des majuscules).
PRONUNCIATION: dict[str, dict[str, str]] = {
    "wolof": {},
    "pulaar": {},
}


class SpeechError(Exception):
    """L'API vocale n'a pas répondu correctement (hors ligne, quota, erreur)."""


def _client() -> httpx.AsyncClient:
    s = get_settings()
    return httpx.AsyncClient(
        base_url=s.speech_api_base_url,
        headers={
            "Authorization": f"Bearer {s.speech_api_key}",
            # Sans User-Agent, le proxy de l'hébergeur bloque la requête.
            "User-Agent": "sunuchat/0.1",
        },
        timeout=60,
    )


async def transcribe(audio: bytes, language: str | None = None, filename: str = "vocal.ogg") -> str:
    language = language or get_settings().default_language
    data = {"model": STT_MODEL, "response_format": "json"}
    if language in {"wolof", "pulaar", "serer"}:
        data["language"] = language
    try:
        async with _client() as client:
            r = await client.post(
                "/audio/transcriptions",
                data=data,
                files={"file": (filename, audio, "application/octet-stream")},
            )
        r.raise_for_status()
        return r.json().get("text", "").strip()
    except (httpx.HTTPError, ValueError) as e:
        raise SpeechError(f"Transcription impossible : {e}") from e


# ---------- Nombres en toutes lettres ----------
# Le TTS lit mal les chiffres. Au Sénégal, on dit les nombres en français même en wolof
# ou en pulaar : on les écrit donc en lettres françaises avant la synthèse. Les numéros
# (4 chiffres ou plus, hors années) se lisent par paires : 1515 → « quinze quinze ».

_UNITS_FR = (
    "zéro un deux trois quatre cinq six sept huit neuf dix onze douze treize quatorze "
    "quinze seize dix-sept dix-huit dix-neuf"
).split()
_TENS_FR = {2: "vingt", 3: "trente", 4: "quarante", 5: "cinquante", 6: "soixante"}
SYMBOLS = {"%": "pour cent", "°C": "degrés", "°": "degrés"}
# Unités écrites après un nombre : (singulier, pluriel). « 0,6 km » → « kilomètre ».
UNITS = {
    "km": ("kilomètre", "kilomètres"),
    "m": ("mètre", "mètres"),
    "kg": ("kilo", "kilos"),
    "mg": ("milligramme", "milligrammes"),
    "ml": ("millilitre", "millilitres"),
}
_DECIMAL = re.compile(r"(?<![\d.,])(\d{1,3})[,.](\d{1,3})(?![\d]|[.,]\d)")


def _decimal_to_words(m: re.Match) -> str:
    """« 0,6 » → « zéro virgule six », « 0,05 » → « zéro virgule zéro cinq »."""
    whole, frac = m.group(1), m.group(2)
    zeros = len(frac) - len(frac.lstrip("0"))
    words = ["zéro"] * zeros + ([number_to_words(int(frac))] if frac.strip("0") else [])
    return f"{number_to_words(int(whole))} virgule {' '.join(words)}"


def _below_100(n: int) -> str:
    if n < 20:
        return _UNITS_FR[n]
    t, u = divmod(n, 10)
    if t in (7, 9):  # soixante-dix, quatre-vingt-onze…
        base = "soixante" if t == 7 else "quatre-vingt"
        return base + (" et " if n == 71 else "-") + _UNITS_FR[10 + u]
    if t == 8:
        return "quatre-vingts" if u == 0 else f"quatre-vingt-{_UNITS_FR[u]}"
    if u == 0:
        return _TENS_FR[t]
    return _TENS_FR[t] + (" et un" if u == 1 else f"-{_UNITS_FR[u]}")


def number_to_words(n: int) -> str:
    """Nombre entier en lettres françaises (jusqu'à 999 999)."""
    if n < 100:
        return _below_100(n)
    if n < 1000:
        h, r = divmod(n, 100)
        head = "cent" if h == 1 else f"{_UNITS_FR[h]} cent" + ("s" if r == 0 else "")
        return head + (f" {_below_100(r)}" if r else "")
    if n < 1_000_000:
        k, r = divmod(n, 1000)
        head = "mille" if k == 1 else f"{number_to_words(k).removesuffix('s')} mille"
        return head + (f" {number_to_words(r)}" if r else "")
    return " ".join(_below_100(int(d)) for d in str(n))


def phone_to_words(num: str) -> str:
    """Numéro lu par paires, comme en français : 1515 → quinze quinze, 05 → zéro cinq."""
    pairs = [num[i : i + 2] for i in range(len(num) % 2, len(num), 2)]
    if len(num) % 2:
        pairs.insert(0, num[0])
    return " ".join(
        f"zéro {_UNITS_FR[int(p[1])]}" if len(p) == 2 and p[0] == "0" else _below_100(int(p))
        for p in pairs
    )


def spell_numbers(text: str) -> str:
    for sym, word in SYMBOLS.items():
        text = re.sub(rf"(?<=\d)\s*{re.escape(sym)}", f" {word}", text)

    def unit(m: re.Match) -> str:
        value = float(m.group(1).replace(",", "."))
        one, many = UNITS[m.group(2).lower()]
        return f"{m.group(1)} {one if value < 2 else many}"

    text = re.sub(
        r"(\d+(?:[,.]\d+)?)\s*(" + "|".join(UNITS) + r")\b", unit, text, flags=re.IGNORECASE
    )
    # Décimales (« 0,6 », « 37.5 ») avant les numéros, sinon « 0,6 » se lit « zéro, six ».
    text = _DECIMAL.sub(_decimal_to_words, text)

    # « 1 200 », « 12 000 » : séparateur de milliers, lu comme un nombre (pas un numéro
    # « 33 869 07 07 », dont les groupes ne font pas tous 3 chiffres).
    text = re.sub(
        r"(?<!\d)(?<!\d )\d{1,3}(?: \d{3})+(?!\d)(?! \d)",
        lambda m: number_to_words(int(m.group(0).replace(" ", ""))),
        text,
    )

    def repl(m: re.Match) -> str:
        num = m.group(0)
        is_year = len(num) == 4 and num[:2] in ("19", "20")
        if (len(num) >= 4 and not is_year) or (len(num) > 1 and num.startswith("0")):
            return phone_to_words(num)
        return number_to_words(int(num))

    return re.sub(r"\d+", repl, text)


_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_URL = re.compile(r"https?://\S+|www\.\S+")
_BULLET = re.compile(r"^\s*(?:[-*•▪◦]|\d+[.)])\s+", re.MULTILINE)


_STRENGTH = ",:;.!?"  # du plus petit au plus grand silence


def apply_pronunciation(text: str, language: str | None) -> str:
    for word, spoken in PRONUNCIATION.get(language or "", {}).items():
        text = re.sub(rf"(?<!\w){re.escape(word)}(?!\w)", spoken, text, flags=re.IGNORECASE)
    return text


def clean_for_speech(text: str, language: str | None = None) -> str:
    """Prépare la réponse de Gemini pour la voix : sans markdown, émojis, liens ni retours
    à la ligne, que le TTS lit comme de longs silences. Les tirets et parenthèses deviennent
    des virgules : une petite respiration, comme à l'oral."""
    text = unicodedata.normalize("NFC", text).replace("’", "'").replace("ʼ", "'")
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = re.sub(r"[*_`#>|~]+", " ", text)
    text = "".join(  # émojis et symboles (sauf °)
        c for c in text if c == "°" or unicodedata.category(c) not in ("So", "Sk", "Cs", "Co")
    )
    text = text.replace("\u200d", "").replace("\ufe0f", "")
    # Puces et retours à la ligne : une ligne = une phrase.
    lines = [ln.strip() for ln in _BULLET.sub("", text).splitlines()]
    text = " ".join(ln if ln[-1] in ".!?:;," else f"{ln}." for ln in lines if ln)
    text = re.sub(r"\s+[—–-]\s+|[()\[\]{}]", ", ", text)
    text = re.sub(r"[\"«»“”]", " ", text)
    text = re.sub(r"\.{2,}|…", ".", text)  # points de suspension = longue pause
    # « !. » « ,. » : on garde le signe qui marque la plus grande pause.
    text = re.sub(
        r"[.!?:;,](?:\s*[.!?:;,])+",
        lambda m: max(m.group(0).replace(" ", ""), key=_STRENGTH.index),
        text,
    )
    text = re.sub(r"\s+([.!?:;,])", r"\1", text)
    text = spell_numbers(text)
    text = apply_pronunciation(text, language)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"^[,;:\s]+", "", text)


def split_text(text: str, limit: int = MAX_TTS_CHARS) -> list[str]:
    """Découpe un texte en morceaux de moins de `limit` caractères, en coupant entre les phrases."""
    sentences = re.split(r"(?<=[.!?…])\s+|\n+", text.strip())
    chunks: list[str] = []
    current = ""
    for sentence in filter(None, (s.strip() for s in sentences)):
        while len(sentence) > limit:  # phrase trop longue : on coupe sur un espace
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            if current:
                chunks.append(current)
                current = ""
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if current and len(current) + 1 + len(sentence) > limit:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


def tighten_silences(samples: array.array, rate: int, channels: int = 1) -> array.array:
    """Coupe le silence au début et à la fin, et raccourcit les pauses de plus de
    MAX_PAUSE_S secondes (échantillons PCM 16 bits entrelacés)."""
    frame = max(1, int(rate * 0.01)) * channels  # fenêtres de 10 ms
    windows = [samples[i : i + frame] for i in range(0, len(samples), frame)]
    loud = [max(map(abs, w), default=0) > SILENCE_THRESHOLD for w in windows]
    if not any(loud):
        return array.array("h")
    first, last = loud.index(True), len(loud) - 1 - loud[::-1].index(True)
    max_quiet = int(MAX_PAUSE_S / 0.01)
    out, quiet = array.array("h"), 0
    for w, is_loud in zip(windows[first : last + 1], loud[first : last + 1], strict=True):
        quiet = 0 if is_loud else quiet + 1
        if quiet <= max_quiet:
            out.extend(w)
    return out


def concat_wavs(wavs: list[bytes], pauses: list[float] | None = None) -> bytes:
    """Nettoie les silences d'un WAV (ou en recolle plusieurs de même format).
    `pauses[i]` : silence (s) inséré avant le morceau i (0,3 s par défaut)."""
    out = io.BytesIO()
    with wave.open(out, "wb") as dst:
        for i, data in enumerate(wavs):
            with wave.open(io.BytesIO(data), "rb") as src:
                params, frames = src.getparams(), src.readframes(src.getnframes())
            if i == 0:
                dst.setparams(params)
            if params.sampwidth != 2:  # format inattendu : on recolle tel quel
                dst.writeframes(frames)
                continue
            samples = array.array("h", frames)
            if sys.byteorder == "big":
                samples.byteswap()
            samples = tighten_silences(samples, params.framerate, params.nchannels)
            if i > 0 and samples:
                gap = pauses[i] if pauses else 0.3
                silence = bytes(2 * int(gap * params.framerate) * params.nchannels)
                samples = array.array("h", silence) + samples

            if sys.byteorder == "big":
                samples.byteswap()
            dst.writeframes(samples.tobytes())
    return out.getvalue()


async def synthesize(text: str, language: str) -> bytes:
    """Lit un texte à voix haute et renvoie un WAV, en une seule requête : la voix enchaîne
    les phrases naturellement et marque les pauses sur la ponctuation. Seule une réponse
    plus longue que la limite de l'API est lue en plusieurs morceaux, coupés entre deux
    phrases."""
    if language not in TTS_VOICES:
        raise SpeechError(f"Pas de voix pour la langue {language}")
    chunks = split_text(clean_for_speech(text, language))
    if not chunks:
        raise SpeechError("Texte vide")
    if len(chunks) > 1:
        log.info("Réponse longue : vocal lu en %d morceaux", len(chunks))
    try:
        async with _client() as client:

            async def speak(chunk: str) -> bytes:
                r = await client.post(
                    "/audio/speech",
                    json={
                        "model": TTS_MODEL,
                        "voice": language,
                        "input": chunk,
                        **VOICE_PARAMS[language],
                    },
                )
                r.raise_for_status()
                return r.content

            wavs = await asyncio.gather(*(speak(x) for x in chunks))
    except httpx.HTTPError as e:
        raise SpeechError(f"Synthèse impossible : {e}") from e
    return concat_wavs(list(wavs))


async def wav_to_ogg(wav: bytes) -> bytes:
    """Convertit un WAV en OGG/Opus, le seul format de vocal accepté par WhatsApp."""
    proc = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-ac",
        "1",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        "-f",
        "ogg",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(wav)
    if proc.returncode != 0:
        raise SpeechError(f"Conversion OGG impossible : {err.decode(errors='ignore')}")
    return out


async def text_to_voice_note(text: str, language: str) -> bytes:
    """Texte → vocal WhatsApp (OGG/Opus)."""
    return await wav_to_ogg(await synthesize(text, language))
