"""Relecture du sérère par un locuteur : c'est elle qui fait progresser le bot.

    uv run python scripts/serer_relecture.py exporter relecture.csv
    uv run python scripts/serer_relecture.py importer relecture.csv "Nom du relecteur"

exporter : un tableur (Excel, Google Sheets) avec les phrases que SunuChat attend
  (assets/serer/phrases.tsv, statut « a_traduire ») et les réponses que le bot a composées
  avec le dictionnaire (data/serer_relecture.jsonl, à rapatrier du serveur). Le locuteur
  écrit la bonne version dans la colonne « sereer_correct », et laisse vide ce qu'il n'a pas
  relu.
importer : chaque ligne relue entre dans phrases.tsv avec le statut « valide ». Le bot
  reprend alors ces phrases telles quelles, et leurs mots rejoignent le vocabulaire accepté.
  Une fois les phrases « urgence_… » validées, les consignes d'urgence partent en sérère au
  lieu du wolof.
"""

import csv
import json
import sys
import unicodedata

from sunuchat import serer
from sunuchat.config import get_settings

COLUMNS = ["id", "francais", "sereer_propose", "retraduction_gemini", "sereer_correct"]


def export(path: str) -> None:
    rows = [
        {"id": p.id, "francais": p.francais, "sereer_correct": ""}
        for p in serer.phrases()
        if p.statut == "a_traduire"
    ]
    log = get_settings().data_dir / "serer_relecture.jsonl"
    seen = set()
    if log.exists():
        for i, line in enumerate(log.read_text(encoding="utf-8").splitlines(), 1):
            essai = json.loads(line)
            if essai["francais"] in seen:
                continue
            seen.add(essai["francais"])
            rows.append(
                {
                    "id": f"bot_{i}",
                    "francais": essai["francais"],
                    "sereer_propose": essai["sereer"],
                    # Ce que Gemini comprend du sérère proposé : aide le relecteur à repérer
                    # un conseil perdu.
                    "retraduction_gemini": essai.get("retraduction", ""),
                    "sereer_correct": "",
                }
            )
    # utf-8-sig : Excel affiche alors correctement ɓ, ɗ, ñ et ŋ.
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, delimiter=";", restval="")
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} phrases à relire dans {path}")


def import_(path: str, relecteur: str) -> None:
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        rows = csv.DictReader(f, delimiter=delimiter)
        relues = [r for r in rows if (r.get("sereer_correct") or "").strip()]
    phrases = {p.id: p for p in serer.phrases()}
    for r in relues:
        sereer = unicodedata.normalize("NFC", r["sereer_correct"].strip())
        phrases[r["id"]] = serer.Phrase(
            id=r["id"],
            francais=r["francais"].strip(),
            sereer=sereer,
            statut="valide",
            source=f"relu par {relecteur}",
        )
    with open(serer.DATA_DIR / "phrases.tsv", "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f, delimiter="\t", lineterminator="\n")
        writer.writerow(["id", "francais", "sereer", "statut", "source"])
        for p in phrases.values():
            writer.writerow([p.id, p.francais, p.sereer, p.statut, p.source])
    print(f"{len(relues)} phrases validées ajoutées à {serer.DATA_DIR / 'phrases.tsv'}")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "exporter":
        export(sys.argv[2])
    elif len(sys.argv) >= 4 and sys.argv[1] == "importer":
        import_(sys.argv[2], sys.argv[3])
    else:
        sys.exit(__doc__)
