"""Nettoie et normalise le corpus kb de SunuChat (sans toucher aux originaux).

Usage (depuis la racine du projet) :
    python3 scripts/nettoyer_kb.py ~/Downloads/kb data/kb
    uv run python -m sunuchat.knowledge build --reset

Produit, dans data/kb/ :
    textes/<id>_<titre>_<annee>.txt  un texte propre par document gardé (UTF-8, NFC, LF)
    chunks.jsonl                     passages découpés (~1000 car., recouvrement 150)
                                     avec métadonnées (id, titre, source, année, page…)
    manifest.csv                     catalogue corrigé + statistiques de nettoyage

Dépendances : Python 3.10+ (bibliothèque standard) et pdftotext (poppler).
"""

import csv
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KB = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / "Downloads" / "kb"
OUT = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else ROOT / "data" / "kb"
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150
MIN_CHUNK = 80

# Documents présents dans le dossier mais absents de manifest.csv : métadonnées ajoutées ici.
EXTRA = {
    "dengue/rapportdesituation3.txt": dict(
        maladie="dengue",
        theme="dengue",
        source="Ministère de la Santé et de l'Action Sociale",
        titre="Épidémie de dengue dans la région de Fatick : "
        "rapport de situation n°3 (1er octobre 2018)",
        annee="2018",
        pays="SN",
    ),
    "dengue/rapportdesituation11.txt": dict(
        maladie="dengue",
        theme="dengue",
        source="Ministère de la Santé et de l'Action Sociale",
        titre="Épidémie de dengue au Sénégal : rapport de situation n°11 (12 novembre 2018)",
        annee="2018",
        pays="SN",
    ),
    "dengue/rapportdesituation12.txt": dict(
        maladie="dengue",
        theme="dengue",
        source="Ministère de la Santé et de l'Action Sociale",
        titre="Épidémie de dengue au Sénégal : rapport de situation n°12 (19 novembre 2018)",
        annee="2018",
        pays="SN",
    ),
    "paludisme/cahierparticipant.txt": dict(
        maladie="paludisme",
        theme="paludisme",
        source="PNLP",
        titre="Cahier du participant : prévention et prise en charge du paludisme (version texte)",
        annee="2015",
        pays="SN",
    ),
    "paludisme/luttecontrepalu3.txt": dict(
        maladie="paludisme",
        theme="paludisme",
        source="PNLP",
        titre="Plan stratégique national de lutte contre le paludisme 2021-2025 "
        "(novembre 2020, copie)",
        annee="2020",
        pays="SN",
    ),
    "dengue/sitrepdenguenum4 (1).pdf": dict(
        maladie="dengue",
        theme="dengue",
        source="Ministère de la Santé Sénégal",
        titre="Rapport de situation dengue n°4 (copie)",
        annee="2018",
        pays="SN",
    ),
    "dengue/bilan_prevention.pdf": dict(
        maladie="dengue",
        theme="prévention (France)",
        source="Ministère de la Santé / Assurance Maladie (France)",
        titre="Mon Bilan Prévention : mes repères pour en parler",
        annee="",
        pays="hors SN (France)",
    ),
}
# Hors sujet : gardé hors de clean/textes, signalé dans le rapport.
OFF_TOPIC = {
    "dengue/bilan_prevention.pdf": "dispositif français « Mon Bilan Prévention » pour adultes, "
    "sans rapport avec la dengue ni le Sénégal"
}

# Ordre de priorité pour les doublons de paragraphes : le document le plus récent garde le texte.
PRIORITY = ["PAL-01", "PAL-17", "PAL-20", "PAL-21", "PAL-18", "PAL-19"]


# ------------------------------------------------------------------ normalisation des caractères

PUA_BULLET = re.compile(r"^[-•▪■□◦●○►▶➢➤✓✔❖·∙]\s*")
BULLETS = re.compile(r"[ \t]*[\ue000-\uf8ff•▪■□◦●○►▶➢➤✓✔❖][ \t]*")
QUOTES = str.maketrans(
    {
        "’": "'",
        "‘": "'",
        "‛": "'",
        "´": "'",
        "`": "'",
        "“": '"',
        "”": '"',
        "„": '"',
        " ": " ",
        " ": " ",
        " ": " ",
        " ": " ",
        "\t": " ",
    }
)
SPACED_PAGE = re.compile(r"P ?a ?g ?e(?: \d)+ s ?u ?r(?: \d)+|Page \d+ sur \d+", re.I)
INVISIBLE = re.compile(r"[­​-‏  ⁠﻿\x00-\x08\x0b\x0e-\x1f\x7f]")


def normalize_chars(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # NFKC : ligatures (ﬁ ﬂ), exposants, espaces spéciaux ; on revient ensuite en NFC.
    text = unicodedata.normalize("NFKC", text)
    text = INVISIBLE.sub("", text)
    text = text.translate(QUOTES)
    text = SPACED_PAGE.sub("", text)
    return unicodedata.normalize("NFC", text)


# ------------------------------------------------------------------ lecture


def read_pages(path: Path) -> list[str]:
    if path.suffix.lower() == ".pdf":
        out = subprocess.run(
            ["pdftotext", "-enc", "UTF-8", str(path), "-"], capture_output=True, check=True
        ).stdout
        pages = out.decode("utf-8", errors="replace").split("\f")
        if pages and not pages[-1].strip():
            pages = pages[:-1]
        return pages
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")
    return text.split("\f") if "\f" in text else [text]


# ------------------------------------------------------------------ nettoyage des lignes

TOC = re.compile(r"(\.{4,}|…{2,}|(\. ){4,})\s*[\divxlcIVXLC]+\s*$")
PAGE_NUM = re.compile(
    r"^(page\s*)?\d{1,4}(\s*(/|sur)\s*\d{1,4})?$|^-\s*\d{1,4}\s*-$|^[ivxlc]{1,6}$", re.I
)
SEPARATOR = re.compile(r"^[\W_]{3,}$")
TERMINAL = (".", "!", "?", ":", ";", "»", '"')


def is_numeric_line(s: str) -> bool:
    chars = s.replace(" ", "")
    letters = sum(c.isalpha() for c in chars)
    return len(chars) >= 3 and letters < 0.4 * len(chars)


def repeated_lines(pages: list[list[str]]) -> set[str]:
    """En-têtes / pieds de page : lignes courtes répétées sur beaucoup de pages."""
    if len(pages) >= 4:
        counts = Counter(ln for lines in pages for ln in set(lines) if ln)
        return {ln for ln, n in counts.items() if n >= max(3, 0.3 * len(pages)) and len(ln) < 150}
    # Texte converti d'un seul bloc : lignes de titre courant répétées.
    counts = Counter(ln for ln in pages[0] if 15 <= len(ln) <= 120)
    return {ln for ln, n in counts.items() if n >= 5}


def clean_lines(pages: list[str]) -> list[tuple[int, str]]:
    # Puces (Wingdings en zone privée Unicode, •, ▪…) : une puce = une nouvelle ligne « - ».
    pages = [BULLETS.sub("\n- ", normalize_chars(p)) for p in pages]
    split = [[re.sub(r" {2,}", " ", ln).strip() for ln in p.split("\n")] for p in pages]
    repeated = repeated_lines(split)
    kept: list[tuple[int, str]] = []
    for page_no, lines in enumerate(split, start=1):
        for ln in lines:
            if not ln:
                kept.append((page_no, ""))
                continue
            if (
                ln in repeated
                or TOC.search(ln)
                or PAGE_NUM.match(ln)
                or SEPARATOR.match(ln)
                or is_numeric_line(ln)
            ):
                continue
            ln = PUA_BULLET.sub("- ", ln)
            ln = re.sub(r"^[-–—]\s+", "- ", ln)
            kept.append((page_no, ln))
        kept.append((page_no, ""))  # fin de page = coupure possible
    return kept


def build_paragraphs(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Recolle les lignes coupées par la mise en page et les mots coupés en fin de ligne."""
    lengths = sorted(len(s) for _, s in lines if s)
    width = lengths[int(0.9 * len(lengths))] if lengths else 80
    paras: list[list] = []  # [page, texte]
    prev_blank, last_len = True, 0  # last_len : longueur de la dernière ligne brute
    for page, s in lines:
        if not s:
            prev_blank = True
            continue
        if paras and not s.startswith("- "):
            last = paras[-1][1]
            starts_lower = s[0].islower() or s[0] in "(,;"
            wrapped = last_len >= 0.7 * width and not last.endswith(TERMINAL)
            if re.search(r"[a-zà-ÿ]-$", last) and s[0].islower():
                paras[-1][1] = last[:-1] + s
                prev_blank, last_len = False, len(s)
                continue
            if (starts_lower and not last.endswith(TERMINAL)) or (wrapped and not prev_blank):
                paras[-1][1] = f"{last} {s}"
                prev_blank, last_len = False, len(s)
                continue
        paras.append([page, s])
        prev_blank, last_len = False, len(s)
    out = []
    for page, p in paras:
        p = re.sub(r" {2,}", " ", p).strip()
        if p:
            out.append((page, p))
    return drop_toc(out)


TOC_TITLE = re.compile(r"^(tables? des mati[eè]res|sommaire|contents?|table of contents)\b", re.I)


def drop_toc(paras: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Retire les sommaires : le titre puis la suite de lignes courtes (intitulés) qui le suit."""
    out, i = [], 0
    while i < len(paras):
        if TOC_TITLE.match(paras[i][1]) and len(paras[i][1]) < 40:
            j = i + 1
            while (
                j < len(paras)
                and len(paras[j][1]) < 160
                and not paras[j][1].endswith((".", "!", "?"))
            ):
                j += 1
            if j - i >= 4:
                i = j
                continue
        out.append(paras[i])
        i += 1
    return out


EN_WORDS = set(
    "the of and to in is that for are with as was were by this from be which have has not".split()
)
FR_WORDS = set(
    "le la les des du de et est que pour dans une un sur au aux par sont qui ne pas".split()
)


def is_english(text: str) -> bool:
    words = re.findall(r"[a-zA-Z]+", text.lower())
    en = sum(w in EN_WORDS for w in words)
    fr = sum(w in FR_WORDS for w in words)
    return len(words) >= 12 and en > 2 * fr


# ------------------------------------------------------------------ découpage


def chunk_paragraphs(paras: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Passages d'environ CHUNK_CHARS caractères, coupés entre paragraphes ou phrases,
    avec un recouvrement de CHUNK_OVERLAP caractères pris sur une frontière de phrase."""
    chunks: list[tuple[int, str]] = []
    cur, cur_page = "", None

    def flush():
        nonlocal cur
        if len(cur) >= MIN_CHUNK:
            chunks.append((cur_page, cur.strip()))
        tail = cur[-CHUNK_OVERLAP:]
        m = re.search(r"(?<=[.!?])\s+", tail)
        cur = tail[m.end() :] if m else ""

    for page, p in paras:
        pieces = [p]
        if len(p) > CHUNK_CHARS:  # paragraphe trop long : on coupe aux phrases
            pieces = [x for x in re.split(r"(?<=[.!?;])\s+(?=[A-ZÀ-Ý0-9«\"(-])", p) if x]
        for piece in pieces:
            while len(piece) > CHUNK_CHARS:  # phrase énorme (liste, tableau aplati)
                cut = piece.rfind(" ", 0, CHUNK_CHARS)
                cut = cut if cut > CHUNK_CHARS // 2 else CHUNK_CHARS
                if cur:
                    flush()
                cur_page = page
                cur = piece[:cut]
                flush()
                piece = piece[cut:].strip()
            if cur and len(cur) + len(piece) + 1 > CHUNK_CHARS:
                flush()
            if not cur.strip():
                cur_page = page
            cur = f"{cur}\n{piece}" if cur.strip() else piece
    if len(cur.strip()) >= MIN_CHUNK:
        chunks.append((cur_page, cur.strip()))
    return chunks


# ------------------------------------------------------------------ doublons


def plain(text: str) -> str:
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def shingles(text: str, n: int = 6) -> set[int]:
    w = plain(text).split()
    return {hash(" ".join(w[i : i + n])) for i in range(0, max(0, len(w) - n + 1))}


def containment(a: set, b: set) -> float:
    return len(a & b) / len(a) if a else 0.0


def slug(text: str, maxlen: int = 90) -> str:
    s = plain(text).replace(" ", "_")
    return s[:maxlen].rstrip("_")


# ------------------------------------------------------------------ programme principal


def load_manifest() -> list[dict]:
    with (KB / "manifest.csv").open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k, v in r.items():
            r[k] = unicodedata.normalize("NFC", v or "").strip()
    return rows


def main() -> None:
    rows = load_manifest()
    by_path = {r["chemin_relatif"]: r for r in rows}
    files = {
        unicodedata.normalize("NFC", str(p.relative_to(KB))): p
        for p in sorted(KB.glob("*/*"))
        if p.suffix.lower() in (".pdf", ".txt") and p.parts[-2] != "clean"
    }

    counters = {"PAL": 21, "DEN": 13, "AUT": 6}
    docs = []
    for rel, path in files.items():
        if rel in by_path:
            meta = dict(by_path[rel])
        else:
            extra = EXTRA.get(
                rel,
                dict(
                    maladie=rel.split("/")[0],
                    theme=rel.split("/")[0],
                    source="",
                    titre=path.stem,
                    annee="",
                    pays="",
                ),
            )
            meta = {k: "" for k in rows[0]}
            meta.update(
                extra,
                chemin_relatif=rel,
                nom_fichier=path.name,
                langue="fr",
                licence="à vérifier",
                format=path.suffix[1:].lower(),
                origine="non cataloguée",
                statut="disponible",
            )
        meta.pop("lien", None)
        meta["_path"] = path
        meta["_md5"] = hashlib.md5(path.read_bytes()).hexdigest()
        docs.append(meta)

    # 1. Extraction + nettoyage
    for d in docs:
        pages = read_pages(d["_path"])
        d["_raw_chars"] = sum(len(p) for p in pages)
        d["_pages"] = len(pages) if d["format"] == "pdf" else None
        d["_paras"] = build_paragraphs(clean_lines(pages))
        d["_text"] = "\n\n".join(p for _, p in d["_paras"])
        d["_sh"] = shingles(d["_text"])
        d["statut_nettoyage"], d["remarques"] = "gardé", ""

    # 2. Hors sujet, doublons exacts et quasi-doublons de documents
    def rank(d):  # on garde de préférence le document catalogué, puis le PDF (numéros de page)
        return (d["origine"] == "non cataloguée", d["format"] != "pdf", -len(d["_text"]))

    seen_md5 = {}
    for d in sorted(docs, key=rank):
        rel = d["chemin_relatif"]
        if rel in OFF_TOPIC:
            d["statut_nettoyage"], d["remarques"] = "écarté : hors sujet", OFF_TOPIC[rel]
        elif d["_md5"] in seen_md5:
            d["statut_nettoyage"] = "écarté : doublon exact"
            d["remarques"] = f"fichier identique à {seen_md5[d['_md5']]}"
        else:
            seen_md5[d["_md5"]] = d.get("id") or rel

    # Un document non catalogué contenu à 80 % dans un autre est écarté ; deux documents
    # catalogués ne sont fusionnés qu'au-delà de 95 % (sinon les paragraphes communs sont
    # retirés à l'étape 4).
    live = [d for d in docs if d["statut_nettoyage"] == "gardé"]
    pairs = []
    for i, a in enumerate(live):
        for b in live[i + 1 :]:
            for x, y in ((a, b), (b, a)):
                c = containment(x["_sh"], y["_sh"])
                if c >= 0.8 and rank(x) > rank(y):
                    pairs.append((c, x, y))
    for c, drop, keep in sorted(pairs, key=lambda t: -t[0]):
        if drop["statut_nettoyage"] != "gardé" or keep["statut_nettoyage"] != "gardé":
            continue
        if drop["origine"] != "non cataloguée" and c < 0.95:
            continue
        drop["statut_nettoyage"] = "écarté : quasi-doublon"
        drop["remarques"] = f"{c:.0%} du texte déjà dans {keep.get('id') or keep['chemin_relatif']}"

    # 3. Identifiants pour les documents non catalogués gardés
    prefix = {"paludisme": "PAL", "dengue": "DEN", "autres": "AUT"}
    for d in docs:
        if not d.get("id") and d["statut_nettoyage"] == "gardé":
            p = prefix[d["maladie"]]
            counters[p] += 1
            d["id"] = f"{p}-{counters[p]:02d}"

    # 4. Paragraphes répétés d'un document à l'autre (le plus prioritaire garde le texte)
    kept = [d for d in docs if d["statut_nettoyage"] == "gardé"]
    kept.sort(
        key=lambda d: (PRIORITY.index(d["id"]) if d["id"] in PRIORITY else len(PRIORITY), d["id"])
    )
    seen_para: dict[str, str] = {}
    for d in kept:
        paras, removed, english = [], 0, 0
        local = set()
        for page, p in d["_paras"]:
            if is_english(p):  # corpus en français : on retire les passages en anglais
                english += 1
                continue
            key = plain(p)
            if len(key) >= 60 and key in local:  # répété dans le même document
                removed += 1
                continue
            local.add(key)
            if len(key) >= 200 and key in seen_para:
                removed += 1
                continue
            if len(key) >= 200:
                seen_para[key] = d["id"]
            paras.append((page, p))
        d["_paras"], d["_dup_paras"], d["_en_paras"] = paras, removed, english

    # 5. Écriture
    (OUT / "textes").mkdir(parents=True, exist_ok=True)
    for old in (OUT / "textes").glob("*.txt"):
        old.unlink()
    all_chunks = []
    for d in sorted(kept, key=lambda d: d["id"]):
        # Première année en fin de nom : c'est celle que l'indexeur sunuchat lit (dernière du nom).
        year = (re.findall(r"(?:19|20)\d{2}", d["annee"]) or [""])[0]
        name = f"{d['id'].lower().replace('-', '_')}_{slug(d['titre'])}" + (
            f"_{year}" if year else ""
        )
        d["fichier_clean"] = f"textes/{name}.txt"
        text = "\n\n".join(p for _, p in d["_paras"]) + "\n"
        (OUT / d["fichier_clean"]).write_text(text, encoding="utf-8")
        chunks = chunk_paragraphs(d["_paras"])
        for i, (page, c) in enumerate(chunks):
            all_chunks.append(
                {
                    "id": hashlib.sha1(f"{d['id']}|{i}|{c}".encode()).hexdigest()[:16],
                    "doc_id": d["id"],
                    "titre": d["titre"],
                    "source": d["source"],
                    "annee": d["annee"],
                    "pays": d["pays"],
                    "theme": d["theme"],
                    "page": page if d["format"] == "pdf" else None,
                    "chunk": i,
                    "texte": c,
                }
            )
        d["nb_caracteres"], d["nb_chunks"] = len(text), len(chunks)

    with (OUT / "chunks.jsonl").open("w", encoding="utf-8") as f:
        for c in all_chunks:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")

    cols = [
        "id",
        "maladie",
        "theme",
        "titre",
        "source",
        "annee",
        "pays",
        "langue",
        "licence",
        "format",
        "chemin_relatif",
        "url_source",
        "origine",
        "statut_nettoyage",
        "fichier_clean",
        "nb_pages",
        "caracteres_bruts",
        "nb_caracteres",
        "nb_chunks",
        "paragraphes_doublons",
        "paragraphes_anglais",
        "remarques",
    ]
    with (OUT / "manifest.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for d in sorted(docs, key=lambda d: (d.get("id") or "ZZZ", d["chemin_relatif"])):
            w.writerow(
                {
                    **d,
                    "nb_pages": d["_pages"] or "",
                    "caracteres_bruts": d["_raw_chars"],
                    "paragraphes_doublons": d.get("_dup_paras", ""),
                    "paragraphes_anglais": d.get("_en_paras", ""),
                    "fichier_clean": d.get("fichier_clean", ""),
                }
            )

    print(f"{len(docs)} fichiers, {len(kept)} gardés, {len(all_chunks)} passages")
    for d in docs:
        if d["statut_nettoyage"] != "gardé":
            print(f"  {d['statut_nettoyage']:28} {d['chemin_relatif']} ({d['remarques']})")


if __name__ == "__main__":
    main()
