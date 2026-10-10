"""Lecture d'une ordonnance envoyée en photo (ou en PDF) : Gemini lit l'image et en tire
les médicaments, les doses, le nombre de prises et la durée. Le résultat, en français, est
ensuite donné à l'agent santé, qui l'explique en vocal dans la langue de l'utilisateur.
"""

import functools
import logging

from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from sunuchat.config import get_settings

log = logging.getLogger(__name__)


class Medicament(BaseModel):
    nom: str = Field(description="Nom du médicament, tel qu'écrit.")
    dosage: str = Field("", description="Dosage ou forme (ex. 500 mg, sirop, comprimé).")
    posologie: str = Field(
        "", description="Combien, combien de fois par jour et à quel moment, tel qu'écrit."
    )
    duree: str = Field("", description="Durée du traitement, si elle est écrite.")
    incertain: bool = Field(False, description="Vrai si une partie est difficile à lire.")


class Ordonnance(BaseModel):
    est_ordonnance: bool = Field(description="Vrai si l'image est une ordonnance médicale.")
    lisible: bool = Field(description="Vrai si les médicaments peuvent être lus.")
    medicaments: list[Medicament] = Field(default_factory=list)
    consignes: str = Field("", description="Autres consignes écrites (repas, repos…).")


PROMPT = """Cette image est envoyée par une personne au Sénégal qui ne sait pas lire. C'est
probablement une ordonnance (souvent manuscrite, en français). Lis-la avec soin et recopie,
pour chaque médicament : le nom, le dosage, la posologie (combien, combien de fois par jour,
à quel moment) et la durée, exactement comme c'est écrit, en développant les abréviations
courantes (cp = comprimé, 3x/j = 3 fois par jour, gél = gélule, sach = sachet, mat/midi/soir).
N'invente rien : si un mot est illisible, mets incertain à vrai. Ignore le nom du patient, du
médecin et les numéros."""


@functools.cache
def _client() -> genai.Client:
    return genai.Client(api_key=get_settings().google_api_key or None)


async def read(data: bytes, mime_type: str) -> Ordonnance:
    """Lit l'ordonnance (image ou PDF)."""
    s = get_settings()
    model = s.translation_model or s.fast_model
    config = types.GenerateContentConfig(
        temperature=0, response_mime_type="application/json", response_schema=Ordonnance
    )
    contents = [types.Part.from_bytes(data=data, mime_type=mime_type), PROMPT]
    models = [model, *s.models_after(model)]
    for i, m in enumerate(models):
        try:
            r = await _client().aio.models.generate_content(
                model=m, contents=contents, config=config
            )
            break
        except Exception:
            # Quota épuisé, surcharge… : modèle suivant ; le dernier échec remonte.
            if i == len(models) - 1:
                raise
            log.warning("%s indisponible pour l'ordonnance : modèle suivant", m)
    if isinstance(r.parsed, Ordonnance):
        return r.parsed
    return Ordonnance.model_validate_json(r.text or "{}")


def as_text(o: Ordonnance) -> str:
    """Contenu de l'ordonnance, en français, pour l'agent santé."""
    lignes = []
    for m in o.medicaments:
        details = ", ".join(x for x in (m.dosage, m.posologie, m.duree) if x)
        ligne = f"{m.nom} : {details}" if details else m.nom
        if m.incertain:
            ligne += " (difficile à lire)"
        lignes.append(ligne)
    if o.consignes:
        lignes.append(f"Consignes : {o.consignes}")
    return (
        "Je t'envoie la photo de mon ordonnance. Voici ce qui est écrit dessus. "
        + " ; ".join(lignes)
        + ". Explique-moi simplement comment prendre mes médicaments."
    )
