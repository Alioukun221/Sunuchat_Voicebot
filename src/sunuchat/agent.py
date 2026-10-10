"""Agent Google ADK : un SequentialAgent en trois étapes, avec le français comme langue pivot.
SunuChat parle seulement wolof et pulaar : ses utilisateurs ne savent souvent pas lire, toutes
ses réponses partent en vocal.

1. comprehension : détecte la langue (wolof ou pulaar) et traduit en français
   (sortie structurée). Un callback mémorise la langue, lance les règles de gravité sur le
   texte français et cherche tout de suite les passages des sources officielles (RAG).
2. sante : raisonne en français. Prompt dynamique construit à chaque tour (profil, alertes,
   passages). Outils : recherche RAG, calendrier PEV, profil, rappels, mémoire longue, et le
   MCP Google Maps si la clé est présente. En cas d'alerte, un callback ajoute toujours le
   message d'urgence, quoi que réponde le modèle.
3. traduction : traduit la réponse en wolof ou en pulaar.
"""

import asyncio
import functools
import logging
import os
import re
from datetime import date
from typing import Literal

from google import genai
from google.adk.agents import LlmAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.readonly_context import ReadonlyContext
from google.adk.apps.app import App, EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.models import Gemini
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.tools import ToolContext, preload_memory
from google.genai import types
from pydantic import BaseModel, Field

from sunuchat import knowledge, memory, places, reminders, rules
from sunuchat.config import get_settings

log = logging.getLogger(__name__)

LANGUES = ("wolof", "pulaar")


def user_language(state) -> str:
    """Langue de l'utilisateur, toujours wolof ou pulaar (un ancien profil peut dire
    « francais » : SunuChat ne répond plus en français)."""
    langue = state.get("user:langue") or get_settings().default_language
    return langue if langue in LANGUES else "wolof"


# Actions WhatsApp décidées pendant un tour (carte du poste de santé, bouton « partager ma
# position », fiche contact du SAMU) : main.py les envoie après la réponse.
_pending_actions: dict[str, list[dict]] = {}


def pop_actions(user_id: str) -> list[dict]:
    return _pending_actions.pop(user_id, [])


_ASKS_FOR_FACILITY = re.compile(
    r"(poste|centre|case) de sant|h[oô]pital|dispensaire|clinique|m[ée]decin le plus proche"
    r"|o[uù] (me )?soigner|o[uù] (aller|consulter)",
    re.IGNORECASE,
)


def asks_for_facility(texte_fr: str) -> bool:
    """La personne cherche où se faire soigner (détecté sur la traduction française)."""
    return bool(_ASKS_FOR_FACILITY.search(texte_fr or ""))


def _setup_env() -> None:
    """ADK lit la clé Gemini dans les variables d'environnement."""
    s = get_settings()
    if s.google_api_key:
        # On écrase toujours : une variable vide héritée du terminal bloquerait ADK.
        os.environ["GOOGLE_API_KEY"] = s.google_api_key
    elif not os.environ.get("GOOGLE_API_KEY"):
        log.error("GOOGLE_API_KEY manquante dans le .env : l'agent Gemini ne peut pas répondre")
    os.environ.setdefault(
        "GOOGLE_GENAI_USE_VERTEXAI", "TRUE" if s.google_genai_use_vertexai else "FALSE"
    )


# Au-delà, on abandonne : un message d'excuse plutôt qu'une attente sans fin.
ANSWER_TIMEOUT_S = 90


def _config(
    model: str, temperature: float = 0.3, thinking: int = 512
) -> types.GenerateContentConfig:
    """Peu de « réflexion » : c'est ce qui coûtait le plus de temps.

    Les modèles 2.5 reçoivent thinking_budget ; les Flash récents (gemini-flash-latest,
    gemini-3…) le refusent parfois (erreur 400) et prennent thinking_level à la place. Les
    « lite » gardent leur réglage par défaut.
    """
    if "lite" in model:
        return types.GenerateContentConfig(temperature=temperature)
    if "2.5" not in model:
        return types.GenerateContentConfig(
            temperature=temperature,
            thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
        )
    return types.GenerateContentConfig(
        temperature=temperature,
        thinking_config=types.ThinkingConfig(thinking_budget=thinking),
    )


# ---------- Étape 1 : compréhension ----------


class Comprehension(BaseModel):
    langue: Literal["wolof", "pulaar"] = Field(description="Langue du message de l'utilisateur.")
    texte_fr: str = Field(description="Traduction fidèle du message en français.")


COMPREHENSION_PROMPT = """Tu reçois le message d'un utilisateur WhatsApp au Sénégal. Il peut être en
wolof, en pulaar (peul), en français, ou un mélange (beaucoup de mots français en wolof).
Il peut venir d'une transcription vocale imparfaite.
1. Détermine la langue de réponse : "wolof" ou "pulaar" (SunuChat ne parle que ces deux
   langues). Indices pulaar : ɓ ɗ ƴ ŋ, "no mbaɗ-ɗaa", "jam tan", "mi", "ko". Indices wolof :
   "na nga def", "dama", "dafa", "lan", "ndax", "yaangi". Si le message est en français, dans
   une autre langue, ou si tu hésites, choisis {langue}.
2. Traduis fidèlement le message en français, sans rien ajouter ni répondre."""


def comprehension_instruction(ctx: ReadonlyContext) -> str:
    return COMPREHENSION_PROMPT.replace("{langue}", user_language(ctx.state))


async def after_comprehension(callback_context: CallbackContext) -> None:
    state = callback_context.state
    result = state.get("comprehension") or {}
    if isinstance(result, str):
        result = Comprehension.model_validate_json(result).model_dump()
    langue = result.get("langue") if result.get("langue") in LANGUES else None
    texte_fr = result.get("texte_fr", "")
    # Une position ou une ordonnance n'a pas de langue : on garde celle de l'utilisateur.
    if langue and not state.get("langue_connue"):
        state["user:langue"] = langue
    else:
        state["user:langue"] = user_language(state)
    state["texte_fr"] = texte_fr
    # Poste de santé demandé sans position ni localité : il faut le bouton de partage.
    state["demander_position"] = (
        asks_for_facility(texte_fr)
        and not state.get("user:position")
        and not state.get("user:localite")
    )
    state["alertes"] = rules.detect_red_flags(texte_fr)
    # RAG pré-chargé : les passages sont donnés directement à l'agent santé.
    try:
        passages = await knowledge.asearch(texte_fr) if texte_fr else []
    except Exception:
        log.exception("Recherche RAG impossible")
        passages = []
    state["passages"] = knowledge.format_passages(passages)


# ---------- Étape 2 : agent santé ----------

SANTE_PROMPT = """Tu es SunuChat, l'assistant santé des familles du Sénégal sur WhatsApp. Tu parles
uniquement wolof et pulaar : beaucoup de tes utilisateurs ne savent pas lire, alors toutes tes
réponses partent en vocal. Tu réponds en français simple : ta réponse sera traduite en {langue}
puis lue à voix haute.

Qui tu es : un agent de santé communautaire bienveillant, patient, qui parle simplement, comme
à quelqu'un de sa famille. Tu ne juges jamais, tu rassures, tu encourages, et tu fais tout
ton possible pour aider toi-même avant d'envoyer vers un médecin.

Ce que tu sais faire (présente-le quand on te salue pour la première fois, quand on te
demande qui tu es ou ce que tu sais faire, en 3 ou 4 phrases et sans liste) :
- répondre aux questions sur toutes les maladies et la santé de la famille : paludisme,
  dengue, vaccins des enfants, mais aussi cancer, diabète, hypertension, grossesse, nutrition,
  santé des femmes (Octobre rose : dépistage du cancer du sein), etc. ;
- expliquer une ordonnance : la personne t'envoie une photo, tu lui dis quels médicaments
  prendre, combien de fois par jour et pendant combien de jours ;
- lui rappeler, en vocal, l'heure de ses médicaments et les vaccins de ses enfants ;
- trouver le poste de santé le plus proche ;
- en cas d'urgence, lui donner le numéro du SAMU, le 1515.
On te parle en vocal : invite la personne à appuyer sur le micro et à te parler dans sa langue.
Si on te demande quelles langues tu parles : wolof et pulaar, rien d'autre. Si on t'écrit en
français, tu réponds quand même en wolof ou en pulaar.

Règles :
- Paludisme, dengue et vaccins : appuie-toi sur les passages des sources officielles ci-dessous
  (Ministère de la Santé, PNLP, OMS, UNICEF) ; si l'information n'y est pas, utilise l'outil
  rechercher_sources.
- Pour tous les autres sujets de santé (cancer, diabète, hypertension, VIH, tuberculose,
  grossesse, Octobre rose, Movember…), et pour l'actualité (épidémie en cours, campagne de
  vaccination ou de dépistage, nouveau traitement, chiffres de l'année), utilise recherche_web.
  Combine les deux quand c'est utile : la consigne officielle d'abord, puis le complément,
  avec sa date et sa source (« d'après le Ministère de la Santé, en octobre 2026… »). Si tu
  ne trouves rien de fiable, dis-le et conseille le poste de santé. N'invente jamais.
- Aide au maximum, comme un agent de santé qui a le temps : explique ce qui se passe
  probablement, ce que la personne peut faire elle-même dès maintenant, et comment savoir si
  ça va mieux. N'envoie pas chez le médecin par réflexe : conseille le poste de santé seulement
  quand c'est nécessaire (signes de gravité, pas d'amélioration après 2 ou 3 jours, bébé de
  moins de 3 mois, femme enceinte, maladie chronique, besoin d'un examen ou d'un médicament sur
  ordonnance), et dis alors pourquoi.
- Soins à la maison et remèdes naturels : propose ce qui est reconnu sans danger, par exemple
  bien boire, se reposer, SRO (sel de réhydratation orale, ou à la maison 1 litre d'eau
  potable, 6 cuillères à café rases de sucre et une demi de sel) en cas de diarrhée, miel
  contre la toux (jamais avant 1 an), gargarisme d'eau tiède salée pour la gorge, linge mouillé
  tiède contre la fièvre, gingembre ou citronnelle en infusion pour les nausées et le rhume,
  alimentation adaptée. Tu peux citer les médicaments courants sans ordonnance (paracétamol,
  SRO, zinc pour la diarrhée de l'enfant) avec la dose habituelle de la notice pour un adulte,
  et pour un enfant dire de demander la dose au pharmacien selon son poids. Jamais
  d'antibiotique sans ordonnance. Déconseille les pratiques dangereuses (scarifications,
  lavements, décoctions inconnues pour les bébés) avec respect.
- Paludisme : la fièvre se soigne à la maison en attendant (paracétamol, boire, linge mouillé),
  mais seul le test (TDR, gratuit au poste de santé) dit si c'est le palu ; le traitement se
  prend après le test, le jour même.
- Tu ne poses pas de diagnostic certain et tu ne changes jamais une dose prescrite par un
  médecin.
- Ordonnance : quand le message contient ce qui est écrit sur une ordonnance, explique chaque
  médicament simplement : son nom, à quoi il sert en général (en une courte phrase), combien
  de fois par jour, à quel moment (matin, midi, soir, avant ou après le repas) et pendant
  combien de jours, exactement comme écrit. Si un passage est illisible ou incertain, dis-le
  et conseille de demander au pharmacien. Termine en proposant un rappel vocal pour chaque
  prise ; si la personne accepte, programme-le avec programmer_rappel.
- Réponses courtes : 3 à 6 phrases (500 caractères environ ; jusqu'à 700 pour une ordonnance),
  sans listes, sans markdown. Écris les nombres et les numéros en chiffres (ex. 1515) : la voix
  les lit en français.
- Ton oral : si la personne a mal ou s'inquiète, commence par une courte phrase qui montre que
  tu as compris (« Je comprends, ça doit être fatigant. »). Phrases courtes, une idée par
  phrase, avec des points : chaque point fait une pause dans le vocal. Termine par un mot
  d'encouragement ou une petite question pour la suite.
- Pour trouver un poste de santé : si la position GPS ou la localité est connue, utilise
  postes_de_sante_proches. Sinon, appelle demander_position (un bouton s'affiche sur WhatsApp
  juste après ton message) et invite la personne à appuyer dessus, sans citer le texte du
  bouton. Donne le nom, le type et la distance des structures trouvées (champ « distance »,
  tel quel), et leur numéro s'il est connu. La carte du plus proche
  est envoyée automatiquement. En cas d'urgence, rappelle aussi le SAMU (1515).
- Si l'utilisateur donne sa localité ou la date de naissance d'un enfant, enregistre-la avec
  memoriser_profil. Pour les vaccins, utilise calendrier_vaccins. Dès qu'une date de
  naissance est enregistrée, un rappel vocal part automatiquement la veille de chaque vaccin :
  dis-le. Si l'utilisateur veut des rappels sans avoir donné la date, demande-la.
- Pour un rappel de médicament, utilise programmer_rappel : le rappel arrive en vocal.
{maps}
Date du jour : {date}
Profil de l'utilisateur : {profil}
{alerte}{demande_position}
Message de l'utilisateur (traduit en français) : {texte_fr}

Passages des sources officielles :
{passages}"""


DEMANDE_POSITION = """
IMPORTANT : on ne connaît ni la position ni la localité de l'utilisateur. Appelle
demander_position. Dans ta réponse, dis-lui simplement, en une ou deux phrases, d'appuyer sur
le bouton qui arrive juste en dessous de ton message, pour que tu voies où il se trouve et
que tu lui indiques le poste de santé le plus proche. Ne cite pas le texte du bouton (pas de
« Envoyer la position ») : parle naturellement, comme à l'oral. Il peut aussi te dire le nom
de son quartier ou de son village. La réponse est souvent écoutée en vocal : sois très concret."""


def sante_instruction(ctx: ReadonlyContext) -> str:
    state = ctx.state
    profil = {
        cle: state.get(f"user:{cle}")
        for cle in ("prenom", "localite", "enfants")
        if state.get(f"user:{cle}")
    }
    if state.get("user:position"):
        profil["position"] = "position GPS partagée sur WhatsApp"
    alertes = state.get("alertes") or []
    alerte = (
        "ALERTE : signes de gravité détectés (" + ", ".join(alertes) + "). Commence par dire "
        "d'aller tout de suite au poste de santé ou à l'hôpital, sans attendre."
        if alertes
        else ""
    )
    maps = (
        "- Tu peux aussi utiliser les outils Google Maps pour chercher un lieu précis."
        if get_settings().google_maps_api_key
        else ""
    )
    return (
        SANTE_PROMPT.replace("{langue}", user_language(state))
        .replace("{maps}", maps)
        .replace("{date}", date.today().strftime("%d/%m/%Y"))
        .replace("{profil}", str(profil or "inconnu"))
        .replace("{alerte}", alerte)
        .replace("{demande_position}", DEMANDE_POSITION if state.get("demander_position") else "")
        .replace("{texte_fr}", state.get("texte_fr", ""))
        .replace("{passages}", state.get("passages", ""))
    )


async def rechercher_sources(question: str) -> dict:
    """Cherche dans les documents officiels (PNLP, Ministère de la Santé, OMS, UNICEF).

    Args:
        question: la question, en français.
    """
    passages = await knowledge.asearch(question, k=4)
    return {"passages": knowledge.format_passages(passages)}


def calendrier_vaccins(date_naissance: str) -> dict:
    """Donne le calendrier vaccinal (PEV Sénégal) d'un enfant : vaccins passés et à venir.

    Args:
        date_naissance: date de naissance de l'enfant, au format AAAA-MM-JJ.
    """
    try:
        return rules.vaccine_schedule(rules.parse_date(date_naissance))
    except ValueError as e:
        return {"erreur": str(e)}


def memoriser_profil(
    tool_context: ToolContext,
    localite: str | None = None,
    date_naissance_enfant: str | None = None,
    prenom: str | None = None,
) -> dict:
    """Enregistre des informations durables sur l'utilisateur (gardées d'un jour à l'autre).

    Args:
        localite: quartier, ville ou village de l'utilisateur.
        date_naissance_enfant: date de naissance d'un enfant, format AAAA-MM-JJ.
        prenom: prénom de l'utilisateur.
    """
    state = tool_context.state
    if localite:
        state["user:localite"] = localite
    if prenom:
        state["user:prenom"] = prenom
    result: dict = {"ok": True}
    if date_naissance_enfant:
        try:
            birth = rules.parse_date(date_naissance_enfant)
        except ValueError as e:
            return {"erreur": str(e)}
        enfants = list(state.get("user:enfants") or [])
        if birth.isoformat() not in enfants:
            enfants.append(birth.isoformat())
        state["user:enfants"] = enfants
        try:
            n = len(reminders.add_vaccine_reminders(tool_context.user_id, birth))
            result["rappels_vaccins_programmes"] = n
        except Exception:
            log.exception("Rappels de vaccins non programmés")
    return result


def programmer_rappel(
    tool_context: ToolContext, medicament: str, heures: list[str], jours: int
) -> dict:
    """Programme un rappel WhatsApp pour prendre un médicament.

    Args:
        medicament: nom du médicament, tel que dit par l'utilisateur.
        heures: heures de prise au format HH:MM (heure de Dakar), par exemple ["08:00", "20:00"].
        jours: nombre de jours de traitement.
    """
    try:
        ids = reminders.add_reminder(tool_context.user_id, medicament, heures, jours)
    except ValueError as e:
        return {"erreur": str(e)}
    return {"ok": True, "rappels": len(ids)}


async def postes_de_sante_proches(tool_context: ToolContext, localite: str | None = None) -> dict:
    """Trouve les postes de santé, centres de santé et hôpitaux les plus proches de
    l'utilisateur (OpenStreetMap). Utilise sa position GPS si elle a été partagée sur WhatsApp,
    sinon la localité.

    Args:
        localite: quartier, ville ou village, si la position GPS n'est pas connue ou si
            l'utilisateur demande un autre endroit.
    """
    state = tool_context.state
    position = state.get("user:position")
    coords = None
    lieu = localite or (None if position else state.get("user:localite"))
    if lieu:
        coords = await places.geocode(lieu)
    elif position:
        coords = (position["lat"], position["lon"])
    if not coords:
        return {"erreur": "position inconnue : utilise demander_position"}
    found = await places.nearby_health_facilities(*coords)
    if not found:
        return {"erreur": "aucune structure de santé trouvée sur OpenStreetMap près d'ici"}
    nearest = found[0]
    _pending_actions.setdefault(tool_context.user_id, []).append(
        {"type": "location", **{k: nearest[k] for k in ("lat", "lon", "nom", "adresse")}}
    )
    return {
        "structures": [
            {k: f[k] for k in ("nom", "type", "distance_km", "adresse", "telephone") if f[k]}
            for f in found
        ],
        "source": "OpenStreetMap (peut être incomplet)",
    }


def demander_position(tool_context: ToolContext) -> dict:
    """Affiche sur WhatsApp un bouton pour que l'utilisateur partage sa position en un clic.
    À utiliser quand on cherche un poste de santé et que la position et la localité sont
    inconnues."""
    _pending_actions.setdefault(tool_context.user_id, []).append({"type": "location_request"})
    return {"ok": True}


WEB_PROMPT = """Tu fais une recherche Google pour SunuChat, un assistant santé au Sénégal.
Privilégie les sources fiables : Ministère de la Santé et de l'Hygiène publique du Sénégal,
PNLP, OMS, UNICEF, Institut Pasteur de Dakar, presse sénégalaise reconnue.
La question peut porter sur n'importe quel sujet de santé (cancer, diabète, hypertension,
grossesse, Octobre rose…). Cherche d'abord les informations les plus récentes concernant le
Sénégal (actualité des 12 derniers mois en priorité), puis les recommandations générales
(OMS, Ministère de la Santé) si la question n'est pas liée à l'actualité.
Réponds en français, en 3 à 6 phrases factuelles, avec la date de l'information et le nom du
site. Si tu ne trouves rien de fiable, dis-le simplement."""


def _web_search_tool():
    """Recherche Google (grounding Gemini), dans un sous-agent : l'outil google_search ne
    peut pas être mélangé aux autres outils dans un même agent."""
    from google.adk.tools import google_search
    from google.adk.tools.google_search_agent_tool import GoogleSearchAgentTool

    s = get_settings()
    return GoogleSearchAgentTool(
        LlmAgent(
            name="recherche_web",
            model=s.fast_model,
            description=(
                "Recherche sur le web (Google) une information de santé absente des documents "
                "officiels : toute maladie (cancer, diabète…), Octobre rose, épidémie en cours, "
                "campagne, actualité."
            ),
            instruction=WEB_PROMPT,
            tools=[google_search],
            generate_content_config=_config(s.translation_model or s.fast_model, temperature=0.2),
            on_model_error_callback=model_fallback,
        )
    )


def _maps_toolset():
    key = get_settings().google_maps_api_key
    if not key:
        return []
    from google.adk.tools.mcp_tool import McpToolset, StreamableHTTPConnectionParams

    return [
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url="https://mapstools.googleapis.com/mcp",
                headers={"X-Goog-Api-Key": key},
            )
        )
    ]


async def after_sante(callback_context: CallbackContext) -> None:
    """Le message d'urgence est toujours présent en cas d'alerte, quoi que réponde le modèle."""
    state = callback_context.state
    alertes = state.get("alertes") or []
    if alertes:
        reponse = state.get("reponse_fr", "")
        state["reponse_fr"] = (
            rules.urgency_message(alertes, get_settings().samu_number) + "\n\n" + reponse
        ).strip()


# ---------- Étape 3 : traduction ----------

TRADUCTION_PROMPT = """Traduis ce message en {langue}, pour un public sénégalais, dans une langue
simple et naturelle, comme on parle au quotidien. Garde le sens exact, en particulier les
consignes d'urgence. Garde les nombres et les numéros en chiffres, comme dans le message :
au Sénégal on les dit en français, et la voix s'en charge.
Garde le ton chaleureux et les formules de politesse habituelles. Garde des phrases courtes,
chacune terminée par un point, et mets une virgule là où l'on reprend son souffle en parlant :
le message est lu à voix haute et c'est la ponctuation qui fait les pauses.
{alphabet}Réponds uniquement avec la traduction.

Message :
{reponse_fr}"""


WOLOF_GUIDE = """Écris en wolof du Sénégal correct et naturel, celui qu'on entend à Dakar, à
la radio ou dans les messages de santé publique : construis les phrases comme un locuteur
wolof, pas en calquant le français mot à mot.
Orthographe officielle (CLAD), indispensable pour que la voix lise sans erreur :
- uniquement les lettres a à b c d e é ë f g i j k l m n ñ ŋ o ó p q r s t u w x y ;
- voyelles longues et consonnes doubles écrites deux fois (jaay, dëgg, feebar, ndokkale) ;
- jamais l'orthographe à la française : u et non ou, c et non tch, ñ et non gn, j et non dj,
  s et non ss ou ç, x pour le son raclé (xam, xale) ;
- exemples : Jërëjëf. Na nga def ? Waaw. Déedéet. Léegi. Sibiru. Xale bi. Ndax.
Garde tels quels les mots français que tout le monde utilise (poste de santé, vaccin,
moustiquaire, comprimé, SAMU) et les noms propres. Pas d'abréviations, de parenthèses, de
tirets ni de guillemets. Relis-toi avant de répondre : chaque mot doit être bien écrit.
"""


def traduction_instruction(ctx: ReadonlyContext) -> str:
    langue = user_language(ctx.state)
    alphabet = (
        "Écris en pulaar du Fuuta Tooro, tel qu'on le parle au Sénégal (vallée du fleuve : "
        "Podor, Matam, Kanel…), avec ses salutations (« No mbaɗ-ɗaa ? », « Jam tan »). "
        "Pas le fulfulde du Niger, du Nigeria ou du Cameroun, ni le pular du Fouta-Djalon "
        "guinéen. Garde les mots français que les Haalpulaar'en utilisent couramment "
        "(poste de santé, moustiquaire, vaccin). Utilise l'orthographe pulaar officielle du "
        "Sénégal avec ses lettres ɓ ɗ ƴ ŋ.\n"
        if langue == "pulaar"
        else WOLOF_GUIDE
    )
    return (
        TRADUCTION_PROMPT.replace("{langue}", langue)
        .replace("{alphabet}", alphabet)
        .replace("{reponse_fr}", ctx.state.get("reponse_fr", ""))
    )


def _translation_config(s) -> types.GenerateContentConfig:
    # Réflexion minimale (128, le plancher de 2.5 Pro) : une traduction n'en a pas besoin.
    return _config(s.translation_model or s.fast_model, temperature=0.2, thinking=128)


@functools.cache
def _genai_client() -> genai.Client:
    # Client gardé en vie : un client temporaire peut être libéré (et sa session HTTP
    # fermée) pendant la requête, d'où « assert self._connector is not None ».
    return genai.Client()


async def _generate_with_fallback(model: str, error: Exception, contents, config) -> LlmResponse:
    """Le modèle `model` a refusé (quota gratuit épuisé, surcharge…) : on essaie les modèles de
    secours un par un. Si tous échouent, l'erreur d'origine remonte et main.py envoie le
    message d'excuse en vocal."""
    for other in get_settings().models_after(model):
        log.warning("%s indisponible (%s) : essai avec %s", model, str(error)[:120], other)
        try:
            r = await _genai_client().aio.models.generate_content(
                model=other, contents=contents, config=config
            )
            return LlmResponse.create(r)
        except Exception as e:
            error = e
    log.error("Aucun modèle de secours n'a répondu")
    raise error


async def model_fallback(
    callback_context: CallbackContext, llm_request, error: Exception
) -> LlmResponse:
    """Même requête (consignes, outils, format de sortie) envoyée à un modèle de secours."""
    config = llm_request.config.model_copy() if llm_request.config else None
    if config is not None:
        config.thinking_config = None  # chaque modèle a ses propres réglages de réflexion
    return await _generate_with_fallback(llm_request.model, error, llm_request.contents, config)


async def translation_fallback(
    callback_context: CallbackContext, llm_request, error: Exception
) -> LlmResponse:
    """Traduction impossible : la consigne est reconstruite depuis l'état, en un seul message,
    et envoyée aux modèles de secours (jamais de réponse en français)."""
    return await _generate_with_fallback(
        llm_request.model,
        error,
        traduction_instruction(callback_context),
        types.GenerateContentConfig(temperature=0.2),
    )


# ---------- Assemblage ----------


def build_app() -> App:
    _setup_env()
    s = get_settings()
    comprehension = LlmAgent(
        name="comprehension",
        model=s.fast_model,
        instruction=comprehension_instruction,
        include_contents="none",
        output_schema=Comprehension,
        output_key="comprehension",
        generate_content_config=_config(s.fast_model, temperature=0),
        after_agent_callback=after_comprehension,
        on_model_error_callback=model_fallback,
    )
    sante = LlmAgent(
        name="sante",
        model=s.agent_model,
        instruction=sante_instruction,
        tools=[
            rechercher_sources,
            calendrier_vaccins,
            memoriser_profil,
            programmer_rappel,
            postes_de_sante_proches,
            demander_position,
            _web_search_tool(),
            preload_memory,
            *_maps_toolset(),
        ],
        output_key="reponse_fr",
        generate_content_config=_config(s.agent_model),
        after_agent_callback=after_sante,
        on_model_error_callback=model_fallback,
    )
    traduction = LlmAgent(
        name="traduction",
        model=s.translation_model or s.fast_model,
        instruction=traduction_instruction,
        include_contents="none",
        output_key="reponse_finale",
        # Réflexion minimale (128, le plancher de 2.5 Pro) : une traduction n'en a pas besoin.
        generate_content_config=_translation_config(s),
        on_model_error_callback=translation_fallback,
    )
    pipeline = SequentialAgent(name="sunuchat", sub_agents=[comprehension, sante, traduction])
    return App(
        name=memory.APP_NAME,
        root_agent=pipeline,
        events_compaction_config=EventsCompactionConfig(
            compaction_interval=8,
            overlap_size=1,
            summarizer=LlmEventSummarizer(llm=Gemini(model=s.fast_model)),
        ),
    )


_runner: Runner | None = None


def runner() -> Runner:
    global _runner
    if _runner is None:
        _runner = Runner(
            app=build_app(),
            session_service=memory.session_service(),
            memory_service=memory.memory_service(),
        )
    return _runner


async def answer(
    user_id: str, text: str, position: dict | None = None, keep_language: bool = False
) -> tuple[str, str]:
    """Fait tourner l'agent sur un message. Renvoie (réponse, langue de l'utilisateur).

    `position` : {"lat", "lon"} quand l'utilisateur vient de partager sa position WhatsApp.
    `keep_language` : le texte n'est pas de l'utilisateur (lecture d'une ordonnance) : on
    garde sa langue au lieu de la détecter.
    """
    session = await memory.get_or_create_session(user_id)
    _pending_actions.pop(user_id, None)
    message = types.Content(role="user", parts=[types.Part(text=text)])
    delta = {"reponse_fr": "", "reponse_finale": "", "alertes": [], "passages": ""}
    delta["langue_connue"] = bool(position) or keep_language
    if position:
        delta["user:position"] = position
    # Jamais d'attente infinie (modèle bloqué) : au-delà, main.py envoie un message d'excuse.
    async with asyncio.timeout(ANSWER_TIMEOUT_S):
        async for event in runner().run_async(
            user_id=user_id,
            session_id=session.id,
            new_message=message,
            # On repart de zéro pour ce tour : jamais de réponse d'un tour précédent.
            state_delta=delta,
        ):
            if event.error_message:
                log.error("Erreur ADK : %s", event.error_message)

    session = await memory.session_service().get_session(
        app_name=memory.APP_NAME, user_id=user_id, session_id=session.id
    )
    state = session.state
    reply = (state.get("reponse_finale") or state.get("reponse_fr") or "").strip()
    if not reply:
        raise RuntimeError("L'agent n'a pas produit de réponse")
    if state.get("alertes"):
        # Le numéro reste en chiffres en fin de message, quelle que soit la langue.
        reply += f"\n\n🚑 SAMU : {get_settings().samu_number}"
    langue = user_language(state)
    actions = _pending_actions.setdefault(user_id, [])
    if not actions and state.get("demander_position"):
        # Le modèle n'a pas pensé au bouton : on l'envoie quand même.
        actions.append({"type": "location_request"})
    if state.get("alertes"):
        actions.append({"type": "samu"})  # fiche contact : appel au SAMU en un appui
    log.info("Actions WhatsApp : %s", [x["type"] for x in actions] or "aucune")
    try:
        await memory.memory_service().add_session_to_memory(session)
    except Exception:
        log.exception("Mémoire longue non mise à jour")
    return reply, langue
