"""Textes fixes de SunuChat en wolof, pulaar et français (accueil, menu, urgence).

À FAIRE RELIRE par un locuteur wolof et un locuteur pulaar du Fuuta : ce sont des
brouillons. Les textes wolof suivent l'orthographe CLAD, les textes pulaar l'orthographe
officielle du Sénégal : la voix les lit mieux ainsi. Les titres de boutons font 20
caractères au plus, ceux du menu 24.
"""

LANGUES = ("wolof", "pulaar", "francais")

# Premier message : avant le choix de la langue, l'accueil est dans les trois langues.
ACCUEIL_CARTE = (
    "Asalaa maalekum ! No mbaɗ-ɗaa ! Bonjour !\n\n"
    "Maa ngi tudd SunuChat. Tànnal sa làkk.\n"
    "Ko SunuChat mi wiyetee. Suɓo ɗemngal maa.\n"
    "Je suis SunuChat. Choisis ta langue."
)
BOUTONS_LANGUE = [
    ("langue_wolof", "Wolof"),
    ("langue_pulaar", "Pulaar"),
    ("langue_francais", "Français"),
]

# Après le choix : lu à voix haute (wolof, pulaar) puis le menu s'affiche.
BIENVENUE = {
    "wolof": (
        "Dalal ak jàmm ! Maa ngi tudd SunuChat. Maa ngi fi ngir dimbali la ci sa wér-gi-yaram "
        "ak sa njaboot : sibiru, dengue, vaccin yu xale yi, ak poste de santé bi gën a jege. "
        "Mën nga ma bind, walla yónnee ma vocal. Tànnal li nga bëgg ci menu bi."
    ),
    "pulaar": (
        "Bisimilla e mon ! Ko SunuChat mi wiyetee. Mi ɗon ngam wallude ma e cellal maa e "
        "cellal galle maa : paludisme, dengue, vaccin sukaaɓe, e poste de santé ɓadiiɗo. "
        "Aɗa waawi winndude kam, walla neldude kam vocal. Suɓo ko njiɗ-ɗaa e menu oo."
    ),
    "francais": (
        "Bienvenue ! Je suis SunuChat. Je t'aide pour ta santé et celle de ta famille : "
        "paludisme, dengue, vaccins des enfants et poste de santé le plus proche. "
        "Tu peux m'écrire ou m'envoyer un vocal. Choisis ce qui t'intéresse dans le menu."
    ),
}

MENU_TEXTE = {
    "wolof": "Lan nga bëgg xam ?",
    "pulaar": "Ko njiɗ-ɗaa anndude ?",
    "francais": "Que veux-tu savoir ?",
}
MENU_BOUTON = {"wolof": "Tànnal", "pulaar": "Suɓo", "francais": "Choisir"}

# (id, titre, description) par langue.
MENU = {
    "wolof": [
        ("menu_palu", "Sibiru (palu)", "Màndarga yi ak naka lañuy moytoo"),
        ("menu_dengue", "Dengue", "Màndarga yi ak naka lañuy moytoo"),
        ("menu_vaccins", "Vaccin xale yi", "Calendrier bi ak rappel yi"),
        ("menu_poste", "Poste de santé", "Bi gën a jege sa kër"),
        ("menu_urgence", "Urgence · SAMU", "Woo SAMU 1515"),
    ],
    "pulaar": [
        ("menu_palu", "Paludisme", "Maandeeji ɗi e no reentorto"),
        ("menu_dengue", "Dengue", "Maandeeji ɗi e no reentorto"),
        ("menu_vaccins", "Vaccin sukaaɓe", "Calendrier e rappel"),
        ("menu_poste", "Poste de santé", "Ɓurɗo ɓadaade galle maa"),
        ("menu_urgence", "Urgence · SAMU", "Noddu SAMU 1515"),
    ],
    "francais": [
        ("menu_palu", "Paludisme", "Les signes et comment s'en protéger"),
        ("menu_dengue", "Dengue", "Les signes et comment s'en protéger"),
        ("menu_vaccins", "Vaccins des enfants", "Le calendrier et les rappels"),
        ("menu_poste", "Poste de santé", "Le plus proche de chez toi"),
        ("menu_urgence", "Urgence · SAMU", "Appeler le SAMU au 1515"),
    ],
}

# Question posée à l'agent santé pour chaque choix du menu (en français : il répond
# ensuite dans la langue choisie).
QUESTIONS_MENU = {
    "menu_palu": "Quels sont les signes du paludisme et comment protéger ma famille ?",
    "menu_dengue": "Quels sont les signes de la dengue et comment s'en protéger ?",
    "menu_vaccins": (
        "Je veux connaître le calendrier de vaccination de mon enfant et recevoir un rappel "
        "avant chaque vaccin."
    ),
    "menu_poste": "Où est le poste de santé le plus proche ?",
}
# Image envoyée avant la réponse (assets/images/<nom>.jpg), si elle existe.
IMAGES_MENU = {
    "menu_palu": "paludisme",
    "menu_dengue": "dengue",
    "menu_vaccins": "vaccins",
    "menu_poste": "poste",
    "menu_urgence": "urgence",
}

URGENCE = {
    "wolof": "Bu dee jàmp bu metti, woo SAMU ci 1515. Bësal fiche bii ngir woo leen.",
    "pulaar": "So ɗum ko caɗeele, noddu SAMU e 1515. Ñoƴƴu fiche ndee ngam noddude ɓe.",
    "francais": "En cas d'urgence, appelle le SAMU au 1515. Appuie sur la fiche pour appeler.",
}

DESINSCRIT = {
    "wolof": "Baax na. Dootuloo jot alerte yi. Bind « alerte » ngir dellu ci.",
    "pulaar": "Ɗum moƴƴii. A heɓataa alerte kadi. Winndu « alerte » ngam artaade.",
    "francais": (
        "C'est noté : tu ne recevras plus les alertes. Écris « alerte » pour les réactiver."
    ),
}
REINSCRIT = {
    "wolof": "Baax na. Dinga jot alerte yi ci jamono nawet.",
    "pulaar": "Ɗum moƴƴii. Aɗa heɓa alerte e ndungu.",
    "francais": "C'est noté : tu recevras de nouveau les alertes de saison.",
}
