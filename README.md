# SunuChat

Assistant santé sur WhatsApp, en **wolof**, **pulaar** et français, par texte ou par vocal.
Il informe sur le paludisme et la dengue à partir de sources officielles (Ministère de la Santé,
PNLP, OMS, UNICEF), donne le calendrier des vaccins (PEV), programme des rappels de médicaments
et oriente tout de suite vers le poste de santé ou le SAMU (1515) en cas de signe de gravité.

## Démarrer

Après `uv sync`, dans VS Code : `Cmd+Shift+P` → « Python: Select Interpreter » →
`.venv/bin/python`. Sinon les imports restent soulignés en jaune.

```bash
uv sync                      # installe Python 3.12 et les dépendances
cp .env.example .env         # (déjà fait si .env existe) puis remplir les secrets
uv run sunuchat              # serveur sur http://localhost:8000
ngrok http 8000              # URL publique pour le webhook Meta
```

Dans Meta (developers.facebook.com → ton app → WhatsApp → Configuration) :
URL de rappel `https://<ton-url-ngrok>/webhook`, jeton de vérification = `WHATSAPP_VERIFY_TOKEN`
du `.env`, puis abonner le champ **messages**. La page de confidentialité demandée par Meta est
servie sur `https://<ton-url-ngrok>/confidentialite`.

Le guide complet pour le numéro WhatsApp est dans [docs/whatsapp_setup.md](docs/whatsapp_setup.md).

## Index RAG (sources officielles)

Les embeddings sont calculés **en local** avec `BAAI/bge-m3` (multilingue, très bon en
français, pas de quota) et stockés dans **ChromaDB** (`data/chroma/`). Au premier lancement, le
modèle est téléchargé depuis Hugging Face (environ 2,3 Go), puis il reste en cache. Sur un Mac
Apple Silicon, il tourne sur le GPU (MPS).

```bash
uv run python -m sunuchat.knowledge list                # documents gardés du corpus kb
uv run python -m sunuchat.knowledge build --limit 300   # petit index pour tester vite
uv run python -m sunuchat.knowledge build               # index complet
uv run python -m sunuchat.knowledge build --reset       # tout réindexer (après changement de modèle)
uv run python -m sunuchat.knowledge search "comment entretenir la moustiquaire"
```

Le corpus est lu dans `KB_DIR` (par défaut `data/kb/`) : `chunks.jsonl` contient les passages
déjà nettoyés et découpés (titre, source, année, page), `textes/` un texte propre par document et
`manifest.csv` le catalogue. Pour le régénérer depuis le dossier brut `~/Downloads/kb`
(il faut `pdftotext` : `brew install poppler`) :

```bash
python3 scripts/nettoyer_kb.py ~/Downloads/kb data/kb
uv run python -m sunuchat.knowledge build --reset
```

On peut aussi ajouter des PDF officiels dans `data/raw/manuel/`. Relancer `build` n'ajoute que les nouveaux passages. Pour changer de
modèle, mettre `EMBEDDING_MODEL=...` dans le `.env` puis `build --reset`.

## Recherche web et postes de santé

- **Recherche web** : quand les documents officiels ne suffisent pas (épidémie en cours,
  campagne, actualité), l'agent santé appelle l'outil `recherche_web` (recherche Google
  intégrée à Gemini, sans clé en plus).
- **Poste de santé le plus proche** : l'utilisateur partage sa position WhatsApp
  (📎 → Position), ou l'agent lui envoie un bouton « Envoyer la position ». Les structures
  proches viennent d'OpenStreetMap ; le bot répond avec les plus proches et envoie la carte
  du premier. Sans position, il se sert de la localité (« Pikine », « Podor »).

## Accueil, menu, SAMU, rappels et alertes

- **Accueil** : au premier message, une carte avec l'image `accueil` et 3 boutons
  Wolof / Pulaar / Français. Après le choix : un vocal de bienvenue dans la langue, puis un
  menu (paludisme, dengue, vaccins, poste de santé, urgence). Un choix du menu est répondu
  en vocal en wolof et en pulaar, précédé de l'image du thème. « menu » rouvre le menu.
  Textes dans `src/sunuchat/textes.py` (brouillons à faire relire par des locuteurs).
- **SAMU** : dès qu'un signe de gravité est détecté, la réponse est suivie d'une fiche
  contact SAMU 1515 : un appui sur « Appeler » lance l'appel.
- **Rappels de vaccins** : quand une date de naissance d'enfant est donnée, un rappel est
  programmé la veille de chaque vaccin du PEV, à 10 h.
- **Alertes d'hivernage** : envoyées le 15 juillet et le 15 septembre à 10 h à toutes les
  personnes inscrites (inscription au choix de la langue, « stop » pour se désinscrire,
  « alerte » pour revenir). Envoi manuel : `uv run python -m sunuchat.campaigns` (ou
  suivi d'un numéro pour tester).

### Modèles de messages Meta (obligatoires pour les rappels et alertes)

Hors des 24 h qui suivent le dernier message d'une personne, WhatsApp n'accepte que des
modèles approuvés. À créer dans WhatsApp Manager → Modèles de message, langue Français,
avec un en-tête **Image** si tu mets l'image correspondante dans `assets/images/`, puis à
reporter dans le `.env` :

- `TEMPLATE_RAPPEL_VACCIN=rappel_vaccin` (catégorie Utilité) :
  « 💉 Rappel SunuChat : demain {{2}}, ton enfant a rendez-vous pour ses vaccins de {{1}}
  ({{3}}). Va au poste de santé avec son carnet de vaccination. »
- `TEMPLATE_ALERTE_SAISON=alerte_hivernage` (catégorie Marketing) :
  « 🦟 C'est l'hivernage : le paludisme et la dengue reviennent. Dors sous une moustiquaire
  imprégnée, vide les eaux stagnantes et va au poste de santé dès la première fièvre.
  Réponds à ce message pour poser tes questions. Écris stop pour ne plus recevoir d'alertes. »

### Images

Déposer dans `assets/images/` (JPG ou PNG, carré 1080 × 1080, moins de 5 Mo) :
`accueil`, `paludisme`, `dengue`, `vaccins`, `poste`, `urgence`, `hivernage`. Une image
absente est simplement sautée. Chaque image n'est envoyée qu'une fois à Meta (identifiant
gardé 25 jours dans `data/images.json`).

## Structure

```
src/sunuchat/
  main.py        serveur FastAPI : /health, /confidentialite et webhook WhatsApp
  whatsapp.py    envoyer des messages et des vocaux, télécharger les vocaux reçus
  agent.py       agent Google ADK (compréhension → santé → traduction) et ses outils
  rules.py       règles fixes : signes de gravité, calendrier PEV
  speech.py      voix : transcription et synthèse wolof / pulaar (API Kiriku)
  knowledge.py   sources officielles : nettoyage, index, recherche
  places.py      postes de santé proches (OpenStreetMap : Nominatim + Overpass, sans clé)
  reminders.py   rappels de médicaments et de vaccins
  onboarding.py  accueil, choix de la langue, menu, urgence (boutons WhatsApp)
  textes.py      textes fixes en wolof, pulaar et français
  campaigns.py   inscrits et alertes de saison (hivernage)
  images.py      visuels de assets/images/, envoyés une fois à Meta puis réutilisés
  memory.py      sessions et mémoire longue par numéro WhatsApp
  config.py      variables d'environnement (.env)
tests/
data/            bases SQLite et ChromaDB (non committés), corpus nettoyé data/kb/
scripts/         nettoyer_kb.py : nettoyage du corpus brut kb → data/kb/
docs/
```

## Chemin d'un message

1. Meta envoie le message sur `POST /webhook` (`main.py`). La signature est vérifiée, on répond
   200 tout de suite et le message passe en coches bleues avec « en train d'écrire ».
2. Si c'est un vocal, il est téléchargé (`whatsapp.py`) et transcrit par Kiriku (`speech.py`).
3. L'agent (`agent.py`) tourne en trois étapes, avec le français comme langue pivot :
   - **comprehension** : Gemini détecte la langue et traduit en français. Les règles de gravité
     (`rules.py`) et la recherche dans les sources (`knowledge.py`) tournent sur ce texte.
   - **sante** : Gemini raisonne en français avec les passages trouvés, le profil de
     l'utilisateur et ses outils (sources, calendrier PEV, profil, rappels, mémoire longue,
     Google Maps si la clé est là). En cas d'alerte, le message d'urgence est toujours ajouté.
   - **traduction** : la réponse est traduite en wolof ou en pulaar (sautée en français).
4. La réponse part en texte, et aussi en vocal si l'utilisateur a envoyé un vocal.

Sessions : une par numéro et par jour (`DatabaseSessionService`, SQLite). Le profil
(`user:langue`, `user:localite`, `user:enfants`) est gardé d'un jour à l'autre. Les anciennes
conversations sont retrouvées avec `SqliteMemoryService` + `preload_memory`, et le contexte est
compressé tous les 8 tours.

En cas de problème, l'utilisateur reçoit un message clair au lieu du silence : « réessaie dans
quelques minutes » si l'agent échoue, « écris-moi ta question » si les vocaux sont indisponibles,
et la réponse en texte seul si la voix de réponse échoue.

## Modèles

Par défaut tout tourne sur `gemini-flash-lite-latest`, qui marche avec le quota gratuit. Avec la
facturation activée sur la clé Gemini, mettre `AGENT_MODEL=gemini-flash-latest` dans le `.env`
pour de meilleures réponses et traductions.

## Tests

```bash
uv run pytest
uv run ruff check src tests
```
