"""Règles fixes, sans IA : signes de gravité et calendrier vaccinal (PEV Sénégal).

Les signes de gravité sont cherchés dans la traduction française du message :
une seule liste de mots-clés couvre donc le wolof, le pulaar et le français.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


# Signes de gravité (paludisme grave, dengue sévère, signes généraux de danger chez l'enfant).
RED_FLAGS: dict[str, list[str]] = {
    "convulsions": ["convuls", "tremble de tout le corps", "secousse"],
    "perte de connaissance": [
        "perte de connaissance",
        "inconscien",
        "evanoui",
        "ne se reveille pas",
        "ne repond plus",
        "coma",
        "tres somnolent",
        "difficile a reveiller",
    ],
    "difficulté à respirer": [
        "difficulte a respirer",
        "respire mal",
        "respire vite",
        "essouffl",
        "etouffe",
        "ne peut pas respirer",
        "respiration difficile",
    ],
    "saignements": [
        "saign",
        "sang dans",
        "vomit du sang",
        "selles noires",
        "urines noires",
        "urine foncee",
        "urines foncees",
        "hemorragie",
        "gencives qui saignent",
    ],
    "vomissements répétés": [
        "vomit tout",
        "vomissements repetes",
        "vomit sans arret",
        "vomit beaucoup",
        "ne peut pas boire",
        "refuse de boire",
        "ne peut plus manger",
        "refuse de teter",
    ],
    "déshydratation / état de choc": [
        "deshydrat",
        "mains froides",
        "pieds froids",
        "ne urine plus",
        "n'urine plus",
        "yeux enfonces",
    ],
    "faiblesse extrême": [
        "ne peut pas se lever",
        "ne tient pas debout",
        "tres faible",
        "paralys",
        "ne peut pas s'asseoir",
    ],
    "fièvre du nourrisson": ["bebe de moins de 2 mois", "nouveau-ne a de la fievre"],
    "jaunisse": ["yeux jaunes", "jaunisse", "peau jaune"],
    "douleur abdominale intense": ["douleur au ventre intense", "tres mal au ventre"],
}


def detect_red_flags(text_fr: str) -> list[str]:
    """Renvoie la liste des signes de gravité trouvés dans un texte en français."""
    text = _normalize(text_fr)
    return [
        label
        for label, keywords in RED_FLAGS.items()
        if any(_normalize(k) in text for k in keywords)
    ]


def urgency_message(flags: list[str], samu: str = "1515") -> str:
    signs = ", ".join(flags)
    return (
        f"⚠️ Signe de gravité ({signs}) : il faut aller TOUT DE SUITE au poste de santé "
        f"ou à l'hôpital le plus proche, sans attendre. Urgence : appelle le SAMU au {samu}."
    )


# Calendrier PEV du Sénégal (avec le vaccin hexavalent introduit en 2025).
# Hexavalent à 6, 10 et 14 semaines et rappel à 15 mois : confirmé (communiqué UNICEF/OMS).
# Le reste est à comparer avec un carnet de vaccination officiel.
@dataclass(frozen=True)
class VaccineDose:
    age_days: int
    age_label: str
    vaccines: str


PEV_CALENDAR: list[VaccineDose] = [
    VaccineDose(0, "à la naissance", "BCG (tuberculose), VPO 0 (polio oral), Hépatite B naissance"),
    VaccineDose(42, "6 semaines", "Hexavalent 1, VPO 1, Pneumocoque 1, Rotavirus 1"),
    VaccineDose(70, "10 semaines", "Hexavalent 2, VPO 2, Pneumocoque 2, Rotavirus 2"),
    VaccineDose(98, "14 semaines", "Hexavalent 3, VPO 3, Pneumocoque 3, VPI 1"),
    VaccineDose(270, "9 mois", "Rougeole-Rubéole 1, Fièvre jaune, Méningite A, VPI 2"),
    VaccineDose(456, "15 mois", "Rougeole-Rubéole 2, rappel Hexavalent"),
]


def parse_date(text: str) -> date:
    """Accepte AAAA-MM-JJ ou JJ/MM/AAAA."""
    text = text.strip()
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text):
        y, mo, d = map(int, m.groups())
    elif m := re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})", text):
        d, mo, y = map(int, m.groups())
    else:
        raise ValueError(f"Date non reconnue : {text}")
    return date(y, mo, d)


def vaccine_schedule(birth: date, today: date | None = None) -> dict:
    """Calendrier d'un enfant : vaccins passés (à vérifier sur le carnet) et à venir."""
    today = today or date.today()
    done, upcoming = [], []
    for dose in PEV_CALENDAR:
        due = birth + timedelta(days=dose.age_days)
        item = {"age": dose.age_label, "date": due.isoformat(), "vaccins": dose.vaccines}
        (done if due < today else upcoming).append(item)
    return {
        "date_naissance": birth.isoformat(),
        "deja_passes_a_verifier_sur_le_carnet": done,
        "a_venir": upcoming,
        "prochain": upcoming[0] if upcoming else None,
    }
