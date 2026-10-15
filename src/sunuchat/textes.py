"""Textes fixes de SunuChat, toujours lus à voix haute : les personnes visées ne savent
souvent pas lire.

À FAIRE RELIRE par un locuteur wolof et un locuteur pulaar du Fuuta : ce sont des
brouillons. Les textes wolof suivent l'orthographe CLAD, les textes pulaar l'orthographe
officielle du Sénégal : la voix les lit mieux ainsi.

Sérère : un texte n'est ajouté qu'une fois traduit par un locuteur (assets/serer/phrases.tsv,
statut « valide »). D'ici là, `texte` donne la version wolof, que la plupart des Sereer
comprennent, avec la voix wolof.
"""

LANGUES = ("wolof", "pulaar", "serer")


def texte(textes: dict[str, str], langue: str) -> tuple[str, str]:
    """(texte, langue de la voix) : la version wolof quand la langue n'a pas encore la sienne."""
    if langue in textes:
        return textes[langue], langue
    return textes["wolof"], "wolof"


ERREUR = {
    "wolof": "Baal ma, mënuma la tontu léegi. Jéemaatal ci ay simili.",
    "pulaar": "Yaafo, mi waawaa jaabaade ma jooni. Eto kadi e nder hojomaaji seeɗa.",
}
VOCAL_KO = {
    "wolof": "Baal ma, déggumaa sa vocal bi. Yónneeti ma ko, te wax ndànk.",
    "pulaar": "Yaafo, mi nanaani vocal maa on. Neldu kadi, haalu seeɗa seeɗa.",
}
TYPE_NON_GERE = {
    "wolof": "Yónnee ma vocal, walla nataalu sa ordonnance. Dinaa la dimbali.",
    "pulaar": "Neldu kam vocal, walla foto ordonnance maa. Mi wallete ma.",
}
ORDONNANCE_ILLISIBLE = {
    "wolof": (
        "Baal ma, mënuma jàng ordonnance bi. Fotowaatal ko ci bërëb bu leer, te bu jege, "
        "walla laajal ko pharmacien bi."
    ),
    "pulaar": (
        "Yaafo, mi waawaa tarde ordonnance ndee. Foto nde kadi e nokku jalbuɗo, ɓadaade, "
        "walla naamno pharmacien on."
    ),
}

# Rappels, lus à voix haute sur une petite animation (voir reminders.py).
RAPPEL_MEDICAMENT = {
    "wolof": "Jamonoy garab gi jot na. Jëlal sa {medicament} léegi. Yàlla na la wéral.",
    "pulaar": "Waktu safaara maa yettii. Ƴaŋ {medicament} maa jooni. Allah hokku ma cellal.",
}
RAPPEL_VACCIN = {
    "wolof": (
        "Ëllëg, {jour}, sa doom am na rendez-vous ngir vaccin yu {age}. Demal poste de santé "
        "bi, te yobu carnet bi."
    ),
    "pulaar": (
        "Jaŋngo, {jour}, ɓiɗɗo maa ina jogii rendez-vous vaccin {age}. Yah poste de santé, "
        "ndaɗndaa carnet makko."
    ),
}

# Alertes de la veille sanitaire (veille.py) : début et fin de chaque alerte, toujours ajoutés
# par le code, pour que la source et l'appel à vérifier ne manquent jamais.
ALERTE_DEBUT = {
    "wolof": "Xibaar bu am solo ci wàllu wér gi.",
    "pulaar": "Kabaaru jojjuɗo e cellal.",
}
CONSEIL_DEBUT = {
    "wolof": "Ndigal ci wàllu wér gi.",
    "pulaar": "Waajuure e cellal.",
}
ALERTE_VERIFIER = {
    "wolof": (
        "Xibaar bii, {source} moo ko génne. Wóorlul ko ci sa poste de santé walla ci benn "
        "agent de santé, balaa ngay jëf. Wax « stop » ngir dootuloo jot alerte yi."
    ),
    "pulaar": (
        "Kabaaru oo, ko {source} yaltini ɗum. Ƴeewto ɗum e poste de santé maa walla e "
        "agent de santé, hade maa huutoraade ɗum. Wiy « stop » ngam haɗde alerte ɗee."
    ),
}

DESINSCRIT = {
    "wolof": "Baax na. Dootuloo jot alerte yi. Wax « alerte » ngir dellu ci.",
    "pulaar": "Ɗum moƴƴii. A heɓataa alerte kadi. Wiy « alerte » ngam artaade.",
}
REINSCRIT = {
    "wolof": "Baax na. Dinga jot alerte yi ci jamono nawet.",
    "pulaar": "Ɗum moƴƴii. Aɗa heɓa alerte e ndungu.",
}

# Changement de langue (« wolof », « pulaar » ou « seereer », dit ou écrit).
LANGUE_CHOISIE = {
    "wolof": "Baax na. Léegi, dinaa la wax ci wolof.",
    "pulaar": "Ɗum moƴƴii. Jooni, mi haalanay ma e pulaar.",
    # Salutation citée par Crétois (« Comment vas-tu aujourd'hui ? ») : la personne entend
    # tout de suite que SunuChat lui parle sérère.
    "serer": "Na fio xani ?",
}
