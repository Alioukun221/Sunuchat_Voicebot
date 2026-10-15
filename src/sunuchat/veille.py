"""Veille sanitaire : SunuChat prend l'initiative, sans attendre qu'on lui écrive.

Toutes les VEILLE_INTERVALLE_H heures, le bot lit les publications officielles (Ministère de
la Santé : actualités et communiqués ; ANACIM : alertes météo ; OMS : épidémies au Sénégal
et chez ses voisins). Gemini garde celles qui demandent à la population d'agir (épidémie,
campagne de vaccination, canicule, inondation…) et rédige un message court, en français, à
partir des seuls faits publiés. Le message est traduit et part en vocal, dans la langue de
chaque inscrit. Il cite toujours sa source et demande toujours de vérifier auprès du poste
de santé : ces deux phrases sont ajoutées par le code (textes.py), jamais par le modèle. Le
lien de la publication suit en texte, pour un proche qui sait lire.

Garde-fous (il n'y a pas de validation humaine) :
- sources officielles seulement ;
- au premier passage sur une source, ce qui est déjà en ligne est noté comme vu, sans envoi ;
- publications de plus de MAX_AGE_DAYS jours ignorées ;
- seulement la gravité 2 ou 3, et jamais deux fois le même sujet sur les mêmes régions en
  COOLDOWN_DAYS jours ;
- VEILLE_MAX_PAR_SEMAINE alertes par personne et par semaine (la gravité 3 passe toujours) ;
- alerte régionale : seulement aux habitants de ces régions et à ceux dont on ne connaît pas
  la région ;
- « stop » désinscrit (campaigns.py), comme pour les alertes d'hivernage.

Hors de la fenêtre de 24 h, WhatsApp n'accepte qu'un modèle approuvé, sans vocal :
TEMPLATE_ALERTE_SANTE ({{1}} sujet, {{2}} source, bouton de réponse « Écouter »). L'alerte
reste alors en attente et part en vocal dès que la personne appuie sur le bouton ou écrit
au bot (deliver_pending, appelé par main.py).

    uv run python -m sunuchat.veille --dry-run --tout --jours 60   # voir, sans rien envoyer
    uv run python -m sunuchat.veille --a 22177xxxxxxx --tout       # essai sur un numéro
    uv run python -m sunuchat.veille                               # un passage normal
"""

import argparse
import asyncio
import email.utils
import functools
import io
import json
import logging
import re
import sqlite3
import ssl
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin

import certifi
import httpx
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from sunuchat import campaigns, textes
from sunuchat.config import ROOT, get_settings

log = logging.getLogger(__name__)

MINISTERE = "Ministère de la Santé"
ANACIM = "ANACIM"
OMS = "OMS"

MINISTERE_RSS = "https://www.sante.gouv.sn/rss.xml"
MINISTERE_COMMUNIQUES = "https://www.sante.gouv.sn/communiques"
ANACIM_RSS = "https://www.anacim.sn/spip.php?page=backend"
OMS_API = (
    "https://www.who.int/api/news/diseaseoutbreaknews?$orderby=PublicationDateAndTime%20desc"
    "&$top=20&$select=Title,PublicationDateAndTime,UrlName,Summary"
)
OMS_ITEM = "https://www.who.int/emergencies/disease-outbreak-news/item/"
# Le serveur de l'ANACIM n'envoie pas son certificat intermédiaire (les navigateurs vont le
# chercher seuls, Python non) : on le fournit. Il est signé par la racine Sectigo R46.
CERTS = (ROOT / "assets" / "certs" / "sectigo_dv_r36.pem",)
HEADERS = {"User-Agent": "sunuchat/0.1 (veille sanitaire, assistant sante WhatsApp, Senegal)"}

MAX_AGE_DAYS = 7
COOLDOWN_DAYS = 7
PENDING_DAYS = 14
# Analyses Gemini par passage, et publications nouvelles lues par source : le coût reste bas.
MAX_ANALYSES = 12
MAX_PAR_SOURCE = 8
MAX_TEXT = 8000

# ANACIM publie surtout la météo du jour : on ne garde que ce qui ressemble à une alerte.
ANACIM_MOTS = (
    "chaleur",
    "canicule",
    "vigilance",
    "alerte",
    "inondation",
    "fortes pluies",
    "pluies intenses",
    "orage",
    "poussi",
    "brume",
    "vent fort",
)
# L'OMS couvre le monde entier : seulement le Sénégal et ses voisins.
OMS_PAYS = ("senegal", "gambia", "mali", "mauritania", "guinea", "west africa")

MOIS = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}


@dataclass
class Publication:
    source: str
    titre: str
    url: str
    date: datetime | None = None
    texte: str = ""
    # Vrai quand le texte du flux suffit (pas besoin d'ouvrir la page).
    complet: bool = False
    extra: dict = field(default_factory=dict)


class Analyse(BaseModel):
    utile: bool = Field(
        description="Vrai seulement si la population doit connaître cette information "
        "maintenant pour se protéger ou agir."
    )
    gravite: int = Field(
        description="3 = danger immédiat au Sénégal ; 2 = à savoir et à faire cette semaine ; "
        "1 = information générale."
    )
    sujet: str = Field(description="Sujet en 2 à 5 mots, en français (ex. Diphtérie).")
    regions: list[str] = Field(
        default_factory=list,
        description="Régions administratives du Sénégal concernées. Vide : tout le pays.",
    )
    message_fr: str = Field("", description="Message à lire à voix haute, en français.")


ANALYSE_PROMPT = """Tu fais la veille sanitaire de SunuChat, un assistant santé vocal en wolof et en
pulaar pour des familles du Sénégal qui, souvent, ne savent pas lire. Voici une publication
officielle de {source}, du {date}.

1. utile : vrai seulement si elle contient une information que la population doit connaître
   maintenant pour se protéger ou agir : épidémie ou cas d'une maladie au Sénégal ou dans un
   pays voisin, campagne de vaccination, de dépistage ou de distribution gratuite, vague de
   chaleur, fortes pluies, inondations, poussière, médicament ou aliment retiré, eau non
   potable. Faux pour la vie de l'administration (nominations, réunions, partenariats,
   inaugurations, formations, appels d'offres, rapports généraux), pour la météo ordinaire et
   pour ce qui se passe loin du Sénégal sans risque pour lui.
2. gravite : 3, 2 ou 1 (voir le schéma).
3. sujet et regions (régions administratives du Sénégal : Dakar, Thiès, Diourbel, Fatick,
   Kaolack, Kaffrine, Kolda, Sédhiou, Ziguinchor, Tambacounda, Kédougou, Matam, Louga,
   Saint-Louis). Liste vide si tout le pays est concerné ou si ce n'est pas précisé.
4. message_fr, seulement si utile : 3 à 5 phrases courtes, en français simple, lues à voix
   haute. Commence par « Selon {source}, ». Dis ce qui se passe et où, puis 2 ou 3 gestes
   concrets pour se protéger, et quand aller au poste de santé. Uniquement les faits de la
   publication et des conseils de prévention reconnus : aucun chiffre, lieu ou traitement
   inventé. Pas de liste, pas d'abréviation, pas de lien, pas de parenthèses.

Titre : {titre}
Texte :
{texte}"""


# ---------- Base de données ----------


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(get_settings().data_dir / "veille.db")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS publications (
            url TEXT PRIMARY KEY, source TEXT, titre TEXT, publie_le TEXT, vue_le TEXT,
            utile INTEGER, gravite INTEGER, sujet TEXT, regions TEXT, message_fr TEXT,
            diffusee_le TEXT
        );
        CREATE TABLE IF NOT EXISTS envois (
            url TEXT, numero TEXT, statut TEXT, le TEXT, PRIMARY KEY (url, numero)
        );
        CREATE TABLE IF NOT EXISTS traductions (
            url TEXT, langue TEXT, texte TEXT, PRIMARY KEY (url, langue)
        );
        """
    )
    return con


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(d: datetime | None) -> str | None:
    return d.isoformat(timespec="seconds") if d else None


def norm(text: str) -> str:
    """« Région de Kédougou » → « kedougou » : pour comparer des noms de régions."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"^\s*region (de |du |d')?", "", text.lower())
    return re.sub(r"[^a-z]+", " ", text).strip()


# ---------- Collecte ----------


def _html_text(html: str) -> str:
    return " ".join(BeautifulSoup(html or "", "html.parser").get_text(" ").split())


def _rss_items(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    items = []
    for item in root.iter("item"):
        when = None
        if (pub := item.findtext("pubDate")) is not None:
            with _ignore():
                when = email.utils.parsedate_to_datetime(pub.strip())
        elif (dc := item.findtext("{http://purl.org/dc/elements/1.1/}date")) is not None:
            with _ignore():
                when = datetime.fromisoformat(dc.strip().replace("Z", "+00:00"))
        if when and when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        items.append(
            {
                "titre": " ".join((item.findtext("title") or "").split()),
                "url": (item.findtext("link") or "").strip(),
                "date": when,
                "texte": _html_text(item.findtext("description") or ""),
            }
        )
    return items


class _ignore:
    """with _ignore(): … : une date illisible ne doit pas tout arrêter."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return exc[0] is not None and issubclass(exc[0], (ValueError, TypeError, IndexError))


def date_fr(text: str) -> datetime | None:
    """« Lundi, décembre 15, 2025 » ou « 15 décembre 2025 » → datetime (UTC)."""
    text = (text or "").lower()
    month = next((n for nom, n in MOIS.items() if nom in text), None)
    numbers = [int(x) for x in re.findall(r"\d+", text)]
    year = next((n for n in numbers if n > 1900), None)
    day = next((n for n in numbers if 1 <= n <= 31), None)
    if not (month and year and day):
        return None
    with _ignore():
        return datetime(year, month, day, tzinfo=UTC)
    return None


async def collect_ministere_rss(client: httpx.AsyncClient) -> list[Publication]:
    r = await client.get(MINISTERE_RSS)
    r.raise_for_status()
    return [
        Publication(MINISTERE, i["titre"], i["url"], i["date"], i["texte"], complet=True)
        for i in _rss_items(r.text)
        if "/Actualites/" in i["url"] or "/actualites/" in i["url"]
    ]


async def collect_ministere_communiques(client: httpx.AsyncClient) -> list[Publication]:
    """La liste des communiqués n'a pas de date : elle est lue sur la page du communiqué."""
    r = await client.get(MINISTERE_COMMUNIQUES)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    pubs, seen = [], set()
    for a in soup.select(".card-title a[href]"):
        url = urljoin(MINISTERE_COMMUNIQUES, a["href"])
        if url not in seen:
            seen.add(url)
            pubs.append(Publication(MINISTERE, " ".join(a.get_text(" ").split()), url))
    return pubs


async def collect_anacim(client: httpx.AsyncClient) -> list[Publication]:
    r = await client.get(ANACIM_RSS)
    r.raise_for_status()
    pubs = []
    for i in _rss_items(r.text):
        haystack = (i["titre"] + " " + i["texte"]).lower()
        if any(mot in haystack for mot in ANACIM_MOTS):
            complet = len(i["texte"]) > 400
            pubs.append(Publication(ANACIM, i["titre"], i["url"], i["date"], i["texte"], complet))
    return pubs


async def collect_oms(client: httpx.AsyncClient) -> list[Publication]:
    r = await client.get(OMS_API)
    r.raise_for_status()
    pubs = []
    for item in r.json().get("value", []):
        titre = item.get("Title", "")
        if not any(pays in titre.lower() for pays in OMS_PAYS):
            continue
        when = None
        with _ignore():
            when = datetime.fromisoformat(item["PublicationDateAndTime"].replace("Z", "+00:00"))
        pubs.append(
            Publication(
                OMS,
                titre,
                OMS_ITEM + item.get("UrlName", ""),
                when,
                _html_text(item.get("Summary", "")),
                complet=True,
            )
        )
    return pubs


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    for cert in CERTS:
        if cert.exists():
            ctx.load_verify_locations(cafile=str(cert))
    return ctx


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=30, headers=HEADERS, follow_redirects=True, verify=_ssl_context()
    )


COLLECTORS = (collect_ministere_rss, collect_ministere_communiques, collect_anacim, collect_oms)


async def collect_all(client: httpx.AsyncClient) -> list[Publication]:
    """Toutes les sources ; une source en panne n'empêche pas les autres."""
    results = await asyncio.gather(*(c(client) for c in COLLECTORS), return_exceptions=True)
    pubs = []
    for collector, result in zip(COLLECTORS, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("Veille : %s en échec (%r)", collector.__name__, result)
            continue
        log.info("Veille : %s → %d publication(s)", collector.__name__, len(result))
        pubs.extend(result[:MAX_PAR_SOURCE])
    return pubs


def _pdf_text(data: bytes, pages: int = 4) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return " ".join(" ".join((page.extract_text() or "").split()) for page in reader.pages[:pages])


async def read_page(client: httpx.AsyncClient, pub: Publication) -> None:
    """Complète la publication avec le texte (et le PDF joint) et la date de sa page."""
    r = await client.get(pub.url)
    r.raise_for_status()
    if "pdf" in r.headers.get("content-type", ""):
        pub.texte = _pdf_text(r.content)
        return
    soup = BeautifulSoup(r.text, "html.parser")
    if pub.date is None and (d := soup.select_one(".date-display-single")):
        pub.date = date_fr(d.get_text())
    for selector in (".field-name-body", ".texte", "article", "main"):
        node = soup.select_one(selector)
        if node and len(node.get_text(strip=True)) > 80:
            pub.texte = " ".join(node.get_text(" ").split())
            break
    # Les communiqués du Ministère sont souvent un simple PDF joint.
    pdf = next(
        (
            urljoin(pub.url, a["href"])
            for a in soup.select("a[href]")
            if a["href"].lower().endswith(".pdf")
            and ("/sites/default/files/" in a["href"] or "IMG/pdf/" in a["href"])
        ),
        None,
    )
    if pdf and len(pub.texte) < 1500:
        try:
            p = await client.get(pdf)
            p.raise_for_status()
            pub.texte = (pub.texte + " " + _pdf_text(p.content)).strip()
        except Exception:
            log.warning("PDF illisible : %s", pdf)


# ---------- Analyse (Gemini) ----------


@functools.cache
def _client() -> genai.Client:
    return genai.Client(api_key=get_settings().google_api_key or None)


async def analyse(pub: Publication) -> Analyse:
    s = get_settings()
    prompt = (
        ANALYSE_PROMPT.replace("{source}", pub.source)
        .replace("{date}", pub.date.strftime("%d/%m/%Y") if pub.date else "date inconnue")
        .replace("{titre}", pub.titre)
        .replace("{texte}", pub.texte[:MAX_TEXT] or "(pas de texte)")
    )
    config = types.GenerateContentConfig(
        temperature=0, response_mime_type="application/json", response_schema=Analyse
    )
    models = [s.agent_model, *s.models_after(s.agent_model)]
    for i, m in enumerate(models):
        try:
            r = await _client().aio.models.generate_content(model=m, contents=prompt, config=config)
            break
        except Exception:
            if i == len(models) - 1:
                raise
            log.warning("%s indisponible pour la veille : modèle suivant", m)
    result = r.parsed if isinstance(r.parsed, Analyse) else Analyse.model_validate_json(r.text)
    result.gravite = min(3, max(1, result.gravite))
    return result


# ---------- Un passage de veille ----------


def _known_sources(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute("SELECT DISTINCT source FROM publications")}


def _is_duplicate(con: sqlite3.Connection, sujet: str, regions: list[str]) -> bool:
    """Même sujet, mêmes régions, déjà diffusé il y a moins de COOLDOWN_DAYS jours."""
    since = _iso(_now() - timedelta(days=COOLDOWN_DAYS))
    key = (norm(sujet), sorted(norm(r) for r in regions))
    for s, regs in con.execute(
        "SELECT sujet, regions FROM publications WHERE diffusee_le >= ?", (since,)
    ):
        if (norm(s), sorted(norm(r) for r in json.loads(regs or "[]"))) == key:
            return True
    return False


async def run_once(
    numeros: list[str] | None = None,
    dry_run: bool = False,
    max_age_days: int = MAX_AGE_DAYS,
    tout: bool = False,
) -> list[dict]:
    """Collecte, analyse et diffuse. Renvoie les publications analysées.

    `numeros` : n'envoie qu'à ces numéros (sans ciblage ni plafond), pour essayer.
    `dry_run` : n'écrit rien et n'envoie rien.
    `tout` : relit aussi les publications déjà vues (et ignore le premier passage muet).
    """
    con = _db()
    async with http_client() as client:
        pubs = await collect_all(client)
        known = _known_sources(con)
        seen = {r[0] for r in con.execute("SELECT url FROM publications")}
        now_iso = _iso(_now())

        if not tout:
            # Premier passage sur une source : tout ce qui est déjà en ligne est « vu ».
            fresh_sources = {p.source for p in pubs} - known
            for p in pubs:
                if p.source in fresh_sources and not dry_run:
                    con.execute(
                        "INSERT OR IGNORE INTO publications (url, source, titre, publie_le, "
                        "vue_le) VALUES (?, ?, ?, ?, ?)",
                        (p.url, p.source, p.titre, _iso(p.date), now_iso),
                    )
            if fresh_sources:
                log.info("Veille : premier passage sur %s, rien n'est envoyé", fresh_sources)
            pubs = [p for p in pubs if p.source not in fresh_sources and p.url not in seen]
        con.commit()

        results = []
        oldest = _now() - timedelta(days=max_age_days)
        for pub in pubs:
            if len(results) >= MAX_ANALYSES:
                break
            if pub.date and pub.date < oldest:
                continue
            if not pub.complet:
                try:
                    await read_page(client, pub)
                except Exception:
                    log.warning("Page illisible : %s", pub.url)
            if pub.date and pub.date < oldest:
                _mark_seen(con, pub, None, dry_run)
                continue
            try:
                result = await analyse(pub)
            except Exception:
                log.exception("Analyse impossible : %s", pub.url)
                continue
            _mark_seen(con, pub, result, dry_run)
            entry = {
                "url": pub.url,
                "source": pub.source,
                "titre": pub.titre,
                "date": pub.date.strftime("%d/%m/%Y") if pub.date else "",
                **result.model_dump(),
            }
            results.append(entry)
            log.info(
                "Veille : %s · utile=%s gravité=%s · %s",
                pub.source,
                result.utile,
                result.gravite,
                pub.titre[:80],
            )
            if not result.utile or result.gravite < 2 or not result.message_fr:
                continue
            if not numeros and _is_duplicate(con, result.sujet, result.regions):
                log.info("Veille : « %s » déjà diffusé récemment", result.sujet)
                continue
            if dry_run:
                continue
            entry["envoyes"] = await diffuse(entry, numeros)
            con.execute(
                "UPDATE publications SET diffusee_le = ? WHERE url = ?", (_iso(_now()), pub.url)
            )
            con.commit()
    con.close()
    return results


def _mark_seen(
    con: sqlite3.Connection, pub: Publication, result: Analyse | None, dry_run: bool
) -> None:
    if dry_run:
        return
    # Jamais de REPLACE : diffusee_le doit survivre (anti-doublon).
    con.execute(
        "INSERT INTO publications (url, source, titre, publie_le, vue_le, utile, gravite, "
        "sujet, regions, message_fr) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(url) DO UPDATE SET publie_le = excluded.publie_le, "
        "vue_le = excluded.vue_le, utile = excluded.utile, gravite = excluded.gravite, "
        "sujet = excluded.sujet, regions = excluded.regions, message_fr = excluded.message_fr",
        (
            pub.url,
            pub.source,
            pub.titre,
            _iso(pub.date),
            _iso(_now()),
            int(result.utile) if result else 0,
            result.gravite if result else None,
            result.sujet if result else None,
            json.dumps(result.regions if result else [], ensure_ascii=False),
            result.message_fr if result else None,
        ),
    )
    con.commit()


# ---------- Diffusion ----------


def concerned(regions_alerte: list[str], region_user: str | None) -> bool:
    """Alerte nationale, ou région inconnue : tout le monde. Sinon, les habitants."""
    if not regions_alerte or not region_user:
        return True
    return norm(region_user) in {norm(r) for r in regions_alerte}


def _sent_this_week(con: sqlite3.Connection, numero: str) -> int:
    since = _iso(_now() - timedelta(days=7))
    return con.execute(
        "SELECT COUNT(*) FROM envois WHERE numero = ? AND le >= ?", (numero, since)
    ).fetchone()[0]


async def _region(numero: str, profile: dict) -> str | None:
    """Région de la personne, déduite une fois de sa localité puis gardée dans son profil."""
    from sunuchat import memory, places

    if profile.get("region"):
        return profile["region"]
    if not profile.get("localite"):
        return None
    region = await places.region_of(profile["localite"])
    if region:
        try:
            await memory.update_profile(numero, region=region)
        except Exception:
            log.exception("Région non enregistrée")
    return region


async def spoken_text(alerte: dict, langue: str) -> str:
    """Alerte complète lue à voix haute : annonce, message traduit, source et vérification."""
    from sunuchat import agent

    con = _db()
    row = con.execute(
        "SELECT texte FROM traductions WHERE url = ? AND langue = ?", (alerte["url"], langue)
    ).fetchone()
    if row:
        traduit = row[0]
    else:
        traduit = await agent.traduire(alerte["message_fr"], langue)
        con.execute(
            "INSERT OR REPLACE INTO traductions VALUES (?, ?, ?)", (alerte["url"], langue, traduit)
        )
        con.commit()
    con.close()
    fin = textes.ALERTE_VERIFIER[langue].format(source=alerte["source"])
    return f"{textes.ALERTE_DEBUT[langue]} {traduit} {fin}"


def source_note(alerte: dict) -> str:
    """Texte écrit qui suit le vocal : la source et son lien, pour vérifier."""
    quand = f", {alerte['date']}" if alerte.get("date") else ""
    return (
        f"📢 Alerte santé SunuChat : {alerte['sujet']}\n"
        f"Source : {alerte['source']}{quand}\n{alerte['url']}\n"
        "⚠️ Vérifie toujours cette information auprès de ton poste de santé."
    )


_voice_cache: dict[tuple[str, str], bytes] = {}


async def send_alert(numero: str, alerte: dict, langue: str) -> None:
    """Vocal dans la langue de la personne, puis la source en texte. La fenêtre de 24 h doit
    être ouverte."""
    from sunuchat import speech, whatsapp

    key = (alerte["url"], langue)
    if key not in _voice_cache:
        _voice_cache[key] = await speech.text_to_voice_note(
            await spoken_text(alerte, langue), langue
        )
    await whatsapp.send_audio(numero, _voice_cache[key])
    await whatsapp.send_text(numero, source_note(alerte))


def _test_numbers() -> list[str]:
    raw = get_settings().veille_test_numeros
    return [n.strip() for n in raw.split(",") if n.strip()]


async def diffuse(alerte: dict, numeros: list[str] | None = None) -> int:
    """Envoie l'alerte aux inscrits concernés. Renvoie le nombre de personnes touchées
    (vocal envoyé ou modèle « Écouter » envoyé)."""
    from sunuchat import memory, onboarding, whatsapp

    s = get_settings()
    forced = numeros or _test_numbers()
    cibles = forced or campaigns.active_subscribers()
    con = _db()
    # Les alertes en attente (deliver_pending) sont relues depuis cette table.
    con.execute(
        "INSERT OR IGNORE INTO publications (url, source, titre, publie_le, vue_le, utile, "
        "gravite, sujet, regions, message_fr) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, ?)",
        (
            alerte["url"],
            alerte["source"],
            alerte["titre"],
            _iso(date_fr_short(alerte.get("date", ""))),
            _iso(_now()),
            alerte["gravite"],
            alerte["sujet"],
            json.dumps(alerte["regions"], ensure_ascii=False),
            alerte["message_fr"],
        ),
    )
    con.commit()  # sinon la base reste verrouillée pendant les envois (traductions)
    touched = 0
    for numero in cibles:
        try:
            profile = await memory.get_profile(numero)
        except Exception:
            log.exception("Profil illisible : %s", numero)
            profile = {}
        langue = onboarding.language_of(profile)
        if not forced:
            if not concerned(alerte["regions"], await _region(numero, profile)):
                continue
            if alerte["gravite"] < 3 and _sent_this_week(con, numero) >= s.veille_max_par_semaine:
                log.info("Veille : plafond hebdomadaire atteint pour %s", numero)
                continue
        statut = "en_attente"
        try:
            if campaigns.window_open(numero) or forced:
                await send_alert(numero, alerte, langue)
                statut = "envoye"
            elif s.template_alerte_sante:
                await whatsapp.send_template(
                    numero,
                    s.template_alerte_sante,
                    s.template_language,
                    [alerte["sujet"], alerte["source"]],
                )
        except Exception:
            log.exception("Alerte non envoyée à %s", numero)
            if forced:
                continue
        if statut == "envoye" or s.template_alerte_sante:
            touched += 1
        con.execute(
            "INSERT OR REPLACE INTO envois VALUES (?, ?, ?, ?)",
            (alerte["url"], numero, statut, _iso(_now())),
        )
        con.commit()
        await asyncio.sleep(0.2)  # rester sous la limite de débit de Meta
    con.close()
    log.info("Veille : « %s » envoyée à %d personne(s)", alerte["sujet"], touched)
    return touched


def _alert_from_row(row: tuple) -> dict:
    url, source, titre, publie_le, sujet, regions, message_fr, gravite = row
    return {
        "url": url,
        "source": source,
        "titre": titre,
        "date": datetime.fromisoformat(publie_le).strftime("%d/%m/%Y") if publie_le else "",
        "sujet": sujet,
        "regions": json.loads(regions or "[]"),
        "message_fr": message_fr,
        "gravite": gravite,
    }


def date_fr_short(text: str) -> datetime | None:
    """« 12/10/2026 » → datetime (UTC)."""
    try:
        return datetime.strptime(text, "%d/%m/%Y").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None


_ALERT_COLUMNS = (
    "p.url, p.source, p.titre, p.publie_le, p.sujet, p.regions, p.message_fr, p.gravite"
)


async def deliver_pending(numero: str, langue: str) -> int:
    """La personne vient d'écrire (ou d'appuyer sur « Écouter ») : les alertes qui
    l'attendaient partent maintenant en vocal. Renvoie le nombre d'alertes envoyées."""
    con = _db()
    since = _iso(_now() - timedelta(days=PENDING_DAYS))
    rows = con.execute(
        f"SELECT {_ALERT_COLUMNS} FROM envois e JOIN publications p ON p.url = e.url "
        "WHERE e.numero = ? AND e.statut = 'en_attente' AND e.le >= ? ORDER BY e.le",
        (numero, since),
    ).fetchall()
    sent = 0
    for row in rows:
        alerte = _alert_from_row(row)
        try:
            await send_alert(numero, alerte, langue)
        except Exception:
            log.exception("Alerte en attente non envoyée à %s", numero)
            continue
        con.execute(
            "UPDATE envois SET statut = 'envoye' WHERE url = ? AND numero = ?",
            (alerte["url"], numero),
        )
        con.commit()
        sent += 1
    con.close()
    return sent


def alertes_en_cours(jours: int = PENDING_DAYS) -> str:
    """Alertes diffusées ces derniers jours, une par ligne, pour le prompt de l'agent."""
    con = _db()
    since = _iso(_now() - timedelta(days=jours))
    rows = con.execute(
        f"SELECT {_ALERT_COLUMNS} FROM publications p WHERE p.diffusee_le >= ? "
        "ORDER BY p.diffusee_le DESC LIMIT 5",
        (since,),
    ).fetchall()
    con.close()
    lignes = []
    for row in rows:
        a = _alert_from_row(row)
        ou = ", ".join(a["regions"]) or "tout le Sénégal"
        lignes.append(f"- {a['sujet']} ({ou}), {a['source']} {a['date']} : {a['message_fr']}")
    return "\n".join(lignes)


# ---------- Planification ----------


async def run_scheduled() -> None:
    if not get_settings().veille_active:
        return
    try:
        await run_once()
    except Exception:
        log.exception("Passage de veille en échec")


def schedule() -> None:
    from sunuchat import reminders

    s = get_settings()
    if not s.veille_active:
        return
    reminders.scheduler().add_job(
        run_scheduled,
        "interval",
        hours=s.veille_intervalle_h,
        next_run_time=datetime.now() + timedelta(minutes=2),
        id="veille-sanitaire",
        replace_existing=True,
    )


def _main() -> None:
    parser = argparse.ArgumentParser(description="Veille sanitaire SunuChat")
    parser.add_argument("--dry-run", action="store_true", help="n'écrit et n'envoie rien")
    parser.add_argument("--a", nargs="+", metavar="NUMERO", help="envoie seulement à ces numéros")
    parser.add_argument("--jours", type=int, default=MAX_AGE_DAYS, help="âge maximal (jours)")
    parser.add_argument("--tout", action="store_true", help="relit aussi les publications vues")
    args = parser.parse_args()
    if args.tout and not (args.dry_run or args.a):
        parser.error("--tout seulement avec --dry-run ou --a (sinon tout repart à tous)")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s : %(message)s")
    results = asyncio.run(run_once(args.a, args.dry_run, args.jours, args.tout))
    for r in results:
        marque = "📢" if r["utile"] and r["gravite"] >= 2 else "  "
        print(f"\n{marque} [{r['source']} {r['date']}] {r['titre']}")
        print(
            f"   utile={r['utile']} gravité={r['gravite']} sujet={r['sujet']!r} "
            f"régions={r['regions']}"
        )
        if r["message_fr"]:
            print(f"   → {r['message_fr']}")
        if "envoyes" in r:
            print(f"   envoyée à {r['envoyes']} personne(s)")
    print(f"\n{len(results)} publication(s) analysée(s).")


if __name__ == "__main__":
    _main()
