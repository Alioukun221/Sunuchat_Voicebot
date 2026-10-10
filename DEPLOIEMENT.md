# Mettre SunuChat en ligne sur AWS, avec déploiement automatique

Ce guide part de zéro. Compte environ une heure la première fois. Ensuite, chaque
changement se met en ligne tout seul.

## Comment ça marche

```
Ton Mac ──git push──▶ GitHub ──▶ GitHub Actions : 1. lance les tests
                                                  2. envoie le code au serveur
                                                  3. redémarre SunuChat
                                                         │
WhatsApp ◀──▶ Meta ◀──── HTTPS ────▶ Serveur AWS Lightsail (Docker)
                                       ├─ Caddy : certificat HTTPS automatique
                                       └─ SunuChat : le bot, ses bases et le modèle
```

- **AWS Lightsail** : un ordinateur loué chez Amazon, allumé 24 h/24, avec une adresse IP
  fixe. Il remplace ton Mac et ngrok. Le plan 4 Go coûte 24 $ par mois, donc tes 100 $
  de crédit couvrent environ 4 mois.
- **Docker** : emballe SunuChat avec tout ce dont il a besoin. Le serveur n'a rien
  d'autre à installer.
- **Caddy** : donne au serveur une adresse en `https://`, exigée par Meta.
- **GitHub Actions** : le robot qui teste et met en ligne chaque changement (le CI/CD).
  Si un test échoue, rien n'est mis en ligne et le bot actuel continue de tourner.

Les secrets (`.env`) et les données (`data/` : conversations, inscrits, index) ne
passent jamais par GitHub. Ils restent sur le serveur.

---

## Étape 1 · Mettre le code sur GitHub (une fois)

1. Crée un compte sur https://github.com si tu n'en as pas.
2. Sur ton Mac, installe l'outil GitHub et connecte-toi :
   ```
   brew install gh
   gh auth login
   ```
   Réponds : GitHub.com → HTTPS → Yes → Login with a web browser.
3. Crée le dépôt privé et envoie le code :
   ```
   cd ~/Sunuchat
   mkdir -p .github/workflows
   mv deploy/github-actions.yml .github/workflows/deploy.yml
   git init -b main
   git add .
   git status
   ```
   La commande `mv` met en place le fichier du déploiement automatique.
   Vérifie que `.env` n'apparaît **pas** dans la liste (il est exclu par `.gitignore`).
   Puis :
   ```
   git commit -m "SunuChat : première version"
   gh repo create sunuchat --private --source . --push
   ```

## Étape 2 · Créer le serveur AWS (une fois)

1. Ouvre https://lightsail.aws.amazon.com et clique sur **Create instance**.
2. **Instance location** : Paris, zone `eu-west-3a` (la plus proche de Dakar).
3. **Pick your instance image** : Linux/Unix → **OS Only** → **Ubuntu 24.04 LTS**.
4. **Choose your instance plan** : General purpose → **4 GB RAM, 2 vCPUs, 80 GB SSD**
   (24 $ / mois).
5. **Identify your instance** : `sunuchat` → **Create instance**. Attends que le statut
   passe à « Running ».
6. Clique sur l'instance → onglet **Networking** :
   - **Attach static IP** → nom `sunuchat-ip` → **Create** : c'est l'adresse fixe du
     serveur. Note-la, par exemple `15.188.10.20`.
   - Dans **IPv4 Firewall** → **Add rule** → Application **HTTPS** → **Create**.
7. Télécharge la clé qui permet de se connecter au serveur : en haut à droite **Account**
   → **SSH keys** → **Download** la clé de la région Paris. Puis, sur ton Mac :
   ```
   mkdir -p ~/.ssh
   mv ~/Downloads/LightsailDefaultKey-eu-west-3.pem ~/.ssh/sunuchat.pem
   chmod 600 ~/.ssh/sunuchat.pem
   ```
8. Pour ne pas avoir de surprise : console AWS → **Billing and Cost Management** →
   **Credits** (tes 100 $ doivent y être) puis **Budgets** → **Create budget** →
   « Monthly cost budget » de 30 $ avec ton e-mail.

Dans la suite, remplace `15.188.10.20` par ton IP.

## Étape 3 · Premier lancement sur le serveur (une fois)

1. Depuis ton Mac, envoie le projet avec tes secrets et tes données :
   ```
   rsync -avz --exclude .venv --exclude .git --exclude __pycache__ --exclude .pytest_cache \
     -e "ssh -i ~/.ssh/sunuchat.pem" ~/Sunuchat/ ubuntu@15.188.10.20:~/sunuchat/
   ```
   (Tape `yes` si on te demande de faire confiance au serveur.)
2. Connecte-toi au serveur et installe Docker :
   ```
   ssh -i ~/.ssh/sunuchat.pem ubuntu@15.188.10.20
   cd ~/sunuchat
   ./deploy/installer_serveur.sh
   exit
   ```
3. Reconnecte-toi, donne son adresse au bot et lance-le :
   ```
   ssh -i ~/.ssh/sunuchat.pem ubuntu@15.188.10.20
   cd ~/sunuchat
   echo "DOMAIN=15.188.10.20.sslip.io" >> .env
   docker compose up -d --build
   docker compose logs -f app
   ```
   `sslip.io` donne gratuitement un nom à ton IP, ce qu'il faut pour le HTTPS. La
   première fois, la construction prend 5 à 10 minutes, puis le modèle bge-m3 (2,3 Go)
   se télécharge. Attends la ligne `Index RAG prêt : 2303 passages`, puis fais Ctrl+C :
   le bot continue de tourner.
4. Vérifie depuis ton Mac :
   ```
   curl https://15.188.10.20.sslip.io/health
   ```
   Réponse attendue : `{"status":"ok"}`.

## Étape 4 · Brancher WhatsApp sur le serveur (une fois)

1. https://developers.facebook.com/apps → ton app → **WhatsApp** → **Configuration** →
   Webhook → **Modifier** :
   - URL de rappel : `https://15.188.10.20.sslip.io/webhook`
   - Jeton de vérification : le même qu'avant (`WHATSAPP_VERIFY_TOKEN` de ton `.env`)
   - **Vérifier et enregistrer**
2. Arrête ngrok et le serveur de ton Mac.
3. Envoie « Salam » au bot : c'est le serveur AWS qui répond.

## Étape 5 · Activer le déploiement automatique (une fois)

Sur GitHub, ouvre ton dépôt `sunuchat` → **Settings** → **Secrets and variables** →
**Actions** → **New repository secret**, et crée ces 3 secrets :

| Nom | Valeur |
|---|---|
| `SERVER_HOST` | l'IP du serveur, ex. `15.188.10.20` |
| `DOMAIN` | ex. `15.188.10.20.sslip.io` |
| `SSH_KEY` | tout le contenu de la clé : `cat ~/.ssh/sunuchat.pem \| pbcopy`, puis colle |

Pour tester : onglet **Actions** → « Tests et déploiement » → **Run workflow**. Les deux
étapes doivent passer au vert.

## Au quotidien : modifier le bot

```
cd ~/Sunuchat
git add .
git commit -m "Ce que j'ai changé"
git push
```

C'est tout : GitHub lance les tests, puis met la nouvelle version en ligne en quelques
minutes. Tu suis l'avancement dans l'onglet **Actions** du dépôt, et GitHub t'envoie un
e-mail si quelque chose échoue. Le bot actuel continue alors de tourner.

Les images de `assets/images/` passent aussi par `git push`. Pour modifier un secret
(`.env`), il faut aller sur le serveur :

```
ssh -i ~/.ssh/sunuchat.pem ubuntu@15.188.10.20
cd ~/sunuchat && nano .env
docker compose up -d
```

## Avant d'ouvrir le bot au public

1. **Jeton permanent** (le jeton actuel expire) :
   https://business.facebook.com/settings → **Utilisateurs** → **Utilisateurs système** →
   **Ajouter** (`sunuchat-bot`, rôle Admin) → **Attribuer des éléments** : ton app et ton
   compte WhatsApp, en contrôle total → **Générer un jeton** : expiration **Jamais**,
   autorisations `whatsapp_business_messaging` et `whatsapp_business_management`. Mets-le
   dans le `.env` du serveur (`WHATSAPP_ACCESS_TOKEN=`) puis `docker compose up -d`.
2. **Mode Live** : developers.facebook.com → ton app → **Paramètres de l'app** →
   **Général** → URL de politique de confidentialité
   `https://15.188.10.20.sslip.io/confidentialite` → puis passe le mode de l'app en
   **Live**. Sans ça, seuls les numéros de test peuvent écrire au bot.
3. **Sauvegardes** : Lightsail → instance → **Snapshots** → active les snapshots
   automatiques (environ 2 $ par mois).

## Commandes utiles sur le serveur

| Besoin | Commande (dans `~/sunuchat`) |
|---|---|
| Voir ce que fait le bot | `docker compose logs -f app` |
| Redémarrer | `docker compose restart app` |
| Alerte d'hivernage à la main | `docker compose exec app python -m sunuchat.campaigns` |
| Réindexer le corpus | `docker compose exec app python -m sunuchat.knowledge build --reset` |
