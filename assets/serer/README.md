# Dictionnaire sérère de SunuChat

Gemini ne parle pas sérère et Kiriku n'a pas de voix sérère. SunuChat parle donc sérère à
partir de ce dictionnaire, et ne dit en sérère que ce qu'il peut dire sans inventer.

## Les fichiers

| Fichier | Contenu |
|---|---|
| `lexique.tsv` | 469 mots et expressions du corps, de la santé et de la maladie, avec leur sens en français, la classe du nom, des exemples et la page de la source. |
| `phrases.tsv` | Phrases complètes. `atteste` : citées par Crétois. `valide` : relues par un locuteur. `a_traduire` : phrases dont SunuChat a besoin, sans traduction pour l'instant. |

**Source** : Père Léonce Crétois, *Un vocabulaire sereer du corps et de la santé*, extrait du
*Dictionnaire sereer-français* (CLAD, 1972-1977), présenté par Charles Becker et Gilbert Faye,
1993. Document numérisé par l'IRD :
https://horizon.documentation.ird.fr/exl-doc/pleins_textes/2021-08/010016377.pdf

Les entrées ont été relevées en lisant le scan page par page (pas de couche texte). La colonne
`page` renvoie à la pagination imprimée du document. Les dialectes sont notés comme chez
Crétois : BA (Baol), DIA (Dyegueme), FAD (Fadiouth), PAL (Palmarin), PC (Petite-Côte),
SIN (Sine). Quelques lectures du scan sont incertaines (`nq` ou `ng`) : la remarque le dit.

## Comment le bot s'en sert

1. **Compréhension** : Kiriku transcrit le vocal en sérère. Gemini reçoit le sens des mots que
   le lexique reconnaît, et traduit le message en français.
2. **Réponse** : l'agent santé écrit un français très simple, puis Gemini compose le texte
   sérère avec les seuls mots du lexique et les phrases attestées ou validées.
3. **Vérifications** (`serer.check` puis `agent.check_meaning`) :
   - chaque mot doit venir du dictionnaire (ou être un mot français repris tel quel, comme
     « poste de santé »), et au moins la moitié des mots doivent être sérères ;
   - retraduit en français, le texte doit garder toutes les consignes de santé.
4. Si une vérification échoue, la réponse part **en wolof**, que la plupart des Sereer
   comprennent. Les consignes d'urgence partent toujours en wolof tant qu'aucune phrase
   `urgence_…` n'est validée.
5. **Voix** : celle du pulaar, avec ses réglages. `x` et `q`, que cette voix n'a jamais
   appris, sont lus `h` et `k`.

Chaque essai est noté dans `data/serer_relecture.jsonl` sur le serveur : le français, le
sérère proposé, la retraduction de Gemini et ce qui manquait.

## Faire progresser le sérère

Au début, beaucoup de réponses partiront en wolof : le lexique de Crétois décrit le corps et
les maladies, mais presque aucun verbe du quotidien (boire, manger, donner). Chaque phrase
relue par un locuteur élargit ce que le bot peut dire.

```bash
# 1. Rapatrier les essais du serveur
scp -i ~/.ssh/sunuchat.pem ubuntu@13.38.193.114:~/sunuchat/data/serer_relecture.jsonl data/
# 2. Préparer le tableur pour le relecteur (Excel ou Google Sheets)
uv run python scripts/serer_relecture.py exporter relecture.csv
# 3. Le relecteur remplit la colonne « sereer_correct », puis :
uv run python scripts/serer_relecture.py importer relecture.csv "Prénom Nom"
# 4. Vérifier, commiter et pousser : le déploiement emporte assets/serer/
```

Priorités pour le relecteur : les phrases `urgence_…` (elles débloquent les urgences en
sérère), puis les messages fixes `erreur_…`, `alertes_…` et `langue_choisie`. Ils restent en
wolof tant qu'ils n'ont pas leur version dans `textes.py`.
