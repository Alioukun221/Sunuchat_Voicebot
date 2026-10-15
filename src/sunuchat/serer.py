"""Sérère : Gemini ne le parle pas et Kiriku n'a pas de voix sérère. On passe donc par un
dictionnaire.

- Le lexique (assets/serer/lexique.tsv) est tiré du « Vocabulaire sereer du corps et de la
  santé » du Père Léonce Crétois (CLAD, éd. Ch. Becker et G. Faye, 1993), relu page par page
  sur le scan de l'IRD. Le numéro de page de chaque entrée renvoie à ce document.
- Les phrases (assets/serer/phrases.tsv) : celles que Crétois cite (statut « atteste »), celles
  qu'un locuteur a relues (« valide ») et celles que SunuChat attend encore d'un locuteur
  (« a_traduire », sans traduction).

Réponse en sérère : l'agent santé écrit en français simple, puis Gemini compose le texte
sérère avec les seuls mots du glossaire et des phrases attestées. `check` vérifie ensuite que
chaque mot vient du dictionnaire (ou est un mot français repris tel quel, comme « poste de
santé ») et que le texte est bien en sérère, et pas du français déguisé. Sinon, la réponse part
en wolof, que la plupart des Sereer comprennent : mieux vaut un wolof juste qu'un sérère
inventé. Chaque essai est noté dans data/serer_relecture.jsonl, pour qu'un locuteur le relise
et enrichisse phrases.tsv.

Voix : celle du pulaar, langue proche, avec ses réglages (speech.py). Elle n'a jamais appris
« q » ni « x », que le pulaar n'écrit pas : `for_voice` les remplace par le son le plus proche.
"""

import csv
import functools
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from sunuchat.config import ROOT, get_settings

log = logging.getLogger(__name__)

DATA_DIR = ROOT / "assets" / "serer"
VALIDATED = ("atteste", "valide")
# Part maximale de mots absents du dictionnaire (fautes de transcription, petits mots
# grammaticaux que Crétois ne cite pas) et part minimale de mots sérères dans la réponse.
MAX_UNKNOWN = 0.1
MIN_SERER = 0.5
# Lettres sérères absentes de l'alphabet pulaar sur lequel la voix a appris.
VOICE_LETTERS = {"x": "h", "q": "k"}
GLOSSARY_SIZE = 60

_WORD = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*")
# Mots français courants, ignorés pour chercher dans le glossaire.
_STOPWORDS = set(
    """a au aux avec ce ces cet cette dans de des du elle elles en est et etre il ils je la le
    les leur lui ma mais me mes moi mon ne nous on ou par pas pour qu que qui sa se ses si son
    sur ta te tes toi ton tu un une vos votre vous y plus tres bien fait faire peut doit avoir
    tout tous toute toutes ca cela aussi alors comme quand chaque""".split()
)


@dataclass(frozen=True)
class Entry:
    sereer: str
    classe: str
    francais: str
    page: str
    remarque: str


@dataclass(frozen=True)
class Phrase:
    id: str
    francais: str
    sereer: str
    statut: str
    source: str


def _rows(name: str) -> list[dict]:
    with open(DATA_DIR / name, encoding="utf-8", newline="") as f:
        return [
            {k: unicodedata.normalize("NFC", (v or "").strip()) for k, v in row.items()}
            for row in csv.DictReader(f, delimiter="\t")
        ]


@functools.cache
def lexicon() -> tuple[Entry, ...]:
    return tuple(Entry(**row) for row in _rows("lexique.tsv") if row["sereer"])


@functools.cache
def phrases() -> tuple[Phrase, ...]:
    return tuple(Phrase(**row) for row in _rows("phrases.tsv"))


def validated_phrases() -> list[Phrase]:
    return [p for p in phrases() if p.statut in VALIDATED and p.sereer]


def words(text: str) -> list[str]:
    """Mots en minuscules ; l'apostrophe reste dans le mot (fo'ooy), le trait d'union coupe."""
    text = unicodedata.normalize("NFC", text or "").lower().replace("’", "'")
    return _WORD.findall(text)


def _fold(word: str) -> str:
    """Mot français sans accents ni marque du pluriel, pour comparer « fièvres » et « fièvre »."""
    word = "".join(
        c for c in unicodedata.normalize("NFD", word) if unicodedata.category(c) != "Mn"
    )
    return word[:-1] if len(word) > 3 and word[-1] in "sx" else word


def _serer_parts(remarque: str) -> list[str]:
    """Expressions sérères d'une remarque : « fi aak : cicatriser ; … » → ["fi aak"]."""
    parts = []
    for segment in remarque.split(";"):
        segment = segment.strip()
        if " : " in segment and not segment.startswith(("dialecte", "tiré de", "lecture")):
            parts.append(segment.split(" : ", 1)[0])
        elif segment.startswith("tiré de : "):
            parts.append(segment.removeprefix("tiré de : ").split("(", 1)[0])
    return parts


@functools.cache
def vocabulary() -> frozenset[str]:
    """Tous les mots sérères connus : entrées, exemples des remarques et phrases validées."""
    vocab: set[str] = set()
    for e in lexicon():
        vocab.update(words(e.sereer))
        for part in _serer_parts(e.remarque):
            vocab.update(words(part))
    for p in validated_phrases():
        vocab.update(words(p.sereer))
    return frozenset(vocab)


def is_known(word: str) -> bool:
    """Mot du dictionnaire, ou forme fléchie d'un mot du dictionnaire (moyana, jirum…)."""
    vocab = vocabulary()
    if word in vocab:
        return True
    return any(
        len(word) - len(root) <= 4 and word.startswith(root)
        for root in vocab
        if len(root) >= 3
    )


@functools.cache
def _french_index() -> dict[str, tuple[Entry, ...]]:
    index: dict[str, list[Entry]] = {}
    for e in lexicon():
        for w in words(e.francais):
            if w not in _STOPWORDS and len(w) > 2:
                index.setdefault(_fold(w)[:6], []).append(e)
    return {k: tuple(v) for k, v in index.items()}


def glossary(texte_fr: str, limit: int = GLOSSARY_SIZE) -> list[Entry]:
    """Entrées du lexique dont le sens français partage des mots avec le texte : les plus
    utiles d'abord (celles qui couvrent le plus de mots du texte)."""
    index = _french_index()
    scores: dict[Entry, int] = {}
    for w in {w for w in words(texte_fr) if w not in _STOPWORDS and len(w) > 2}:
        for e in index.get(_fold(w)[:6], ()):
            scores[e] = scores.get(e, 0) + 1
    ranked = sorted(scores, key=lambda e: (-scores[e], len(e.francais)))
    return ranked[:limit]


def format_glossary(entries: list[Entry]) -> str:
    lines = []
    for e in entries:
        line = f"- {e.francais} → {e.sereer}"
        if e.classe:
            line += f" ({e.classe})"
        if e.remarque:
            line += f" ; {e.remarque}"
        lines.append(line)
    return "\n".join(lines) or "(aucun mot trouvé)"


def has_urgent_phrases() -> bool:
    """Un locuteur a validé les consignes d'urgence (phrases « urgence_… » de phrases.tsv)."""
    return any(p.id.startswith("urgence_") for p in validated_phrases())


def format_phrases() -> str:
    return "\n".join(f"- {p.francais} → {p.sereer}" for p in validated_phrases())


def gloss(texte_sereer: str) -> str:
    """Pour comprendre un message sérère : le sens des mots que le dictionnaire reconnaît."""
    lines, seen = [], set()
    for w in words(texte_sereer):
        if w in seen or len(w) < 3:  # petits mots grammaticaux : sens trop incertain
            continue
        seen.add(w)
        # D'abord une entrée d'un seul mot, puis une forme fléchie (moyana → moyan), et
        # seulement ensuite une expression qui contient le mot (« naa » seul ne veut pas dire
        # « comment va ta santé ? »).
        heads = [e for e in lexicon() if words(e.sereer) == [w]]
        inflected = [
            e
            for e in lexicon()
            if len(words(e.sereer)) == 1
            and len(e.sereer) >= 3
            and w.startswith(words(e.sereer)[0])
            and len(w) - len(e.sereer) <= 4
        ]
        in_phrase = [e for e in lexicon() if w in words(e.sereer)] if len(w) >= 4 else []
        matches = heads or inflected or in_phrase
        if matches:
            sens = " / ".join(dict.fromkeys(e.francais for e in matches[:3]))
            lines.append(f"- {w} : {sens}")
    return "\n".join(lines)


@dataclass
class Verdict:
    ok: bool
    unknown: list[str]
    serer_share: float


def check(texte_sereer: str, texte_fr: str) -> Verdict:
    """Le texte n'emploie que des mots du dictionnaire ou des mots français repris du message
    d'origine, et il est en sérère pour au moins MIN_SERER de ses mots."""
    ws = words(texte_sereer)
    if not ws:
        return Verdict(False, [], 0.0)
    # Côté français, l'apostrophe coupe le mot : « d'eau » → « eau ».
    french = set(words(texte_fr.replace("'", " ").replace("’", " ")))
    # Un mot repris du français (« poste », « vaccin ») ne compte pas comme du sérère.
    serer = [w for w in ws if w not in french and is_known(w)]
    unknown = [w for w in ws if not is_known(w) and w not in french]
    share = len(serer) / len(ws)
    ok = len(unknown) / len(ws) <= MAX_UNKNOWN and share >= MIN_SERER
    return Verdict(ok, list(dict.fromkeys(unknown)), round(share, 2))


def for_voice(text: str) -> str:
    """Orthographe sérère → lettres que la voix pulaar sait lire."""
    return "".join(VOICE_LETTERS.get(c, VOICE_LETTERS.get(c.lower(), c)) for c in text)


def log_attempt(texte_fr: str, texte_sereer: str, verdict: Verdict, fidelite=None) -> None:
    """Garde chaque essai pour la relecture par un locuteur (scripts/serer_relecture.py)."""
    try:
        path = get_settings().data_dir / "serer_relecture.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            record = {
                "date": datetime.now().isoformat(timespec="seconds"),
                "francais": texte_fr,
                "sereer": texte_sereer,
                "accepte": verdict.ok and bool(fidelite and fidelite.fidele),
                "mots_inconnus": verdict.unknown,
                "part_sereer": verdict.serer_share,
            }
            if fidelite is not None:  # retraduction par Gemini (agent.check_meaning)
                record["retraduction"] = fidelite.retraduction
                record["consignes_essentielles_perdues"] = fidelite.consignes_essentielles_perdues
                record["contresens"] = fidelite.contresens
                record["details_perdus"] = fidelite.details_perdus
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("Essai sérère non enregistré")
