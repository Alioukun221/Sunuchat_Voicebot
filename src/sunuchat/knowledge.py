"""Sources officielles : téléchargement, nettoyage, index (modèle local + ChromaDB) et recherche.

Construire l'index (à refaire quand on ajoute des documents) :

    uv run python -m sunuchat.knowledge build              # corpus kb + pages web
    uv run python -m sunuchat.knowledge build --limit 300  # petit index pour tester vite
    uv run python -m sunuchat.knowledge build --reset      # tout réindexer
    uv run python -m sunuchat.knowledge search "comment entretenir la moustiquaire"

Les documents viennent de trois endroits :
- le corpus nettoyé data/kb/chunks.jsonl (KB_DIR, produit par scripts/nettoyer_kb.py),
  filtré par KB_KEEP ; à défaut, les PDF/TXT bruts d'un dossier kb ;
- des PDF déposés à la main dans data/raw/manuel/ ;
- quelques pages web officielles (WEB_SOURCES).
"""

import argparse
import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

from sunuchat.config import get_settings

log = logging.getLogger(__name__)

# Documents du corpus kb gardés : ceux qui répondent aux questions des familles.
# Un fichier est gardé si son nom contient un de ces mots (sans accents, minuscules).
KB_KEEP = [
    "psnep",  # plan stratégique paludisme 2026-2030
    "prise en charge",
    "prise_en_charge",
    "prevention",
    "diagnostic",
    "guide technique",
    "guide_technique",
    "sensibilisation",
    "communautaire",
    "entreprise",
    "cps",  # chimioprévention du paludisme saisonnier
    "chimioprevention",
    "dengue",
    "tuberculose",
]
# Écartés : chiffres datés, anciens plans, hors Sénégal, surveillance, ancien PEV.
KB_SKIP = [
    "bulletin",
    "sitrep",
    "rapport de situation",
    "rapport_de_situation",
    "antilles",
    "burkina",
    "surveillance",
    "pev 2012",
    "pev_2012",
    "plan pev",
    "plan_pev",
    "ppac",
    "amerique",  # dengue aux Antilles : protocoles hors Sénégal
]

WEB_SOURCES = [
    ("OMS - Paludisme", "https://www.who.int/fr/news-room/fact-sheets/detail/malaria"),
    (
        "OMS - Dengue",
        "https://www.who.int/fr/news-room/fact-sheets/detail/dengue-and-severe-dengue",
    ),
    (
        "UNICEF/OMS - Vaccin hexavalent au Sénégal",
        "https://www.unicef.org/senegal/communiqu%C3%A9s-de-presse/communiqu%C3%A9-conjoint-unicef-oms-le-s%C3%A9n%C3%A9gal-introduit-le-vaccin-hexavalent-dans",
    ),
    ("PNLP Sénégal", "https://www.pnlp.sn/"),
]

CHUNK_CHARS = 1000
CHUNK_OVERLAP = 150
EMBED_BATCH = 64


@dataclass
class Passage:
    text: str
    source: str
    year: str = ""
    page: int | None = None

    def citation(self) -> str:
        parts = [self.source]
        if self.year:
            parts.append(self.year)
        if self.page:
            parts.append(f"p. {self.page}")
        return ", ".join(parts)


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def _year(name: str) -> str:
    years = re.findall(r"(?:19|20)\d{2}", name)
    return years[-1] if years else ""


# ---------- Nettoyage ----------

_TOC_LINE = re.compile(r"\.{4,}\s*\d+\s*$|…{2,}\s*\d+\s*$")
_PAGE_NUM = re.compile(r"^\s*(page\s*)?\d{1,3}(\s*/\s*\d{1,3})?\s*$", re.IGNORECASE)


def clean_pages(pages: list[str]) -> list[str]:
    """Retire en-têtes/pieds de page répétés, sommaires, numéros de page, tableaux de chiffres,
    recolle les mots coupés et les lignes cassées."""
    line_counts: Counter[str] = Counter()
    for page in pages:
        line_counts.update({ln.strip() for ln in page.splitlines() if ln.strip()})
    repeated = {ln for ln, n in line_counts.items() if len(pages) >= 4 and n >= 0.3 * len(pages)}

    cleaned = []
    for page in pages:
        lines = []
        for ln in page.splitlines():
            s = ln.strip()
            if not s or s in repeated or _TOC_LINE.search(s) or _PAGE_NUM.match(s):
                continue
            letters = sum(c.isalpha() for c in s)
            if letters < 0.4 * len(s):  # ligne de tableau ou de chiffres
                continue
            lines.append(s)
        text = "\n".join(lines)
        text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)  # mots coupés en fin de ligne
        text = re.sub(r"(?<![.!?:;])\n(?=[a-zà-ÿ(])", " ", text)  # lignes cassées
        text = re.sub(r"[ \t]+", " ", text).strip()
        cleaned.append(text)
    return cleaned


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    text = text.strip()
    if len(text) <= size:
        return [text] if len(text) > 80 else []
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = max(text.rfind(". ", start, end), text.rfind("\n", start, end))
            if cut > start + size // 2:
                end = cut + 1
        piece = text[start:end].strip()
        if len(piece) > 80:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


# ---------- Lecture des documents ----------


def read_pdf(path: Path) -> list[Passage]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
        pages = [(p.extract_text() or "") for p in reader.pages]
    except Exception as e:  # PDF abîmé ou protégé
        log.warning("PDF illisible %s : %s", path.name, e)
        return []
    passages = []
    name, year = path.stem.replace("_", " "), _year(path.stem)
    for page_no, text in enumerate(clean_pages(pages), start=1):
        for chunk in chunk_text(text):
            passages.append(Passage(chunk, name, year, page_no))
    return passages


def kept(name: str) -> bool:
    name = _plain(name)
    return not any(s in name for s in KB_SKIP) and any(k in name for k in KB_KEEP)


def kb_files(kb_dir: Path) -> list[Path]:
    if not kb_dir.exists():
        return []
    return [
        path
        for path in sorted(kb_dir.rglob("*"))
        if path.suffix.lower() in {".pdf", ".txt", ".md"} and kept(path.name)
    ]


def read_clean_chunks(path: Path) -> list[Passage]:
    """Passages déjà nettoyés et découpés (chunks.jsonl), filtrés sur le titre du document."""
    passages = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        if not kept(c["titre"]):
            continue
        source = f"{c['titre']} ({c['source']})" if c.get("source") else c["titre"]
        years = re.findall(r"(?:19|20)\d{2}", c.get("annee") or "")  # « 2021-2025 » → 2021
        year = years[0] if years else ""
        passages.append(Passage(c["texte"], source, year, c.get("page")))
    return passages


def read_text_file(path: Path) -> list[Passage]:
    text = path.read_text(errors="ignore")
    return [Passage(c, path.stem, _year(path.stem)) for c in chunk_text(clean_pages([text])[0])]


async def fetch_web(name: str, url: str) -> list[Passage]:
    try:
        async with httpx.AsyncClient(
            timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 sunuchat"}
        ) as client:
            r = await client.get(url)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.warning("Source web indisponible %s : %s", url, e)
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
        tag.decompose()
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = "\n".join(
        el.get_text(" ", strip=True) for el in main.find_all(["h1", "h2", "h3", "p", "li"])
    )
    return [Passage(c, name, _year(r.text[:5000])) for c in chunk_text(text)]


async def collect_passages(kb_dir: Path, web: bool = True) -> list[Passage]:
    s = get_settings()
    passages: list[Passage] = []
    chunks = kb_dir / "chunks.jsonl"
    if chunks.exists():
        passages = read_clean_chunks(chunks)
        log.info("%s : %d passages", chunks, len(passages))
        files = []
    else:
        files = kb_files(kb_dir)
    files += sorted((s.data_dir / "raw" / "manuel").glob("*.pdf"))
    for path in files:
        found = read_pdf(path) if path.suffix.lower() == ".pdf" else read_text_file(path)
        log.info("%s : %d passages", path.name, len(found))
        passages.extend(found)
    if web:
        for name, url in WEB_SOURCES:
            found = await fetch_web(name, url)
            log.info("%s : %d passages", name, len(found))
            passages.extend(found)
    # Doublons exacts
    seen, unique = set(), []
    for p in passages:
        key = hashlib.sha1(p.text.encode()).hexdigest()
        if key not in seen:
            seen.add(key)
            unique.append(p)
    return unique


# ---------- Embeddings (modèle local) et base vectorielle ChromaDB ----------
#
# Le modèle est téléchargé
# une seule fois depuis Hugging Face (environ 2,3 Go) puis gardé en cache.
# Les passages et leurs vecteurs sont stockés dans ChromaDB (data/chroma/).

COLLECTION = "sources_officielles"

_model = None
_collection = None


def embedding_model():
    """Charge le modèle d'embeddings une seule fois (sur le GPU du Mac si possible)."""
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        name = get_settings().embedding_model
        log.info("Chargement du modèle d'embeddings %s…", name)
        _model = SentenceTransformer(name, device=get_settings().embedding_device or None)
    return _model


def collection():
    global _collection
    if _collection is None:
        import chromadb

        path = get_settings().data_dir / "chroma"
        client = chromadb.PersistentClient(path=str(path))
        _collection = client.get_or_create_collection(COLLECTION, metadata={"hnsw:space": "cosine"})
    return _collection


def embed_texts(texts: list[str]) -> list[list[float]]:
    vectors = embedding_model().encode(
        texts,
        batch_size=16,
        normalize_embeddings=True,
        show_progress_bar=len(texts) > 64,
    )
    return vectors.tolist()


def _passage_id(p: Passage) -> str:
    return hashlib.sha1(f"{p.source}|{p.page}|{p.text}".encode()).hexdigest()


def build_index(passages: list[Passage], limit: int | None = None, reset: bool = False) -> int:
    """Calcule les embeddings des nouveaux passages et les ajoute à ChromaDB.

    Les passages déjà présents ne sont pas recalculés : on peut relancer sans tout refaire.
    """
    global _collection
    if reset:
        import chromadb

        client = chromadb.PersistentClient(path=str(get_settings().data_dir / "chroma"))
        if COLLECTION in [c.name for c in client.list_collections()]:
            client.delete_collection(COLLECTION)
        _collection = None
    if limit:
        passages = passages[:limit]
    col = collection()
    ids = [_passage_id(p) for p in passages]
    existing: set[str] = set()
    for i in range(0, len(ids), 500):
        existing.update(col.get(ids=ids[i : i + 500], include=[])["ids"])
    todo = [(i, p) for i, p in zip(ids, passages, strict=True) if i not in existing]
    log.info(
        "%d passages, %d déjà indexés, %d à calculer",
        len(passages),
        len(existing),
        len(todo),
    )
    for start in range(0, len(todo), EMBED_BATCH):
        batch = todo[start : start + EMBED_BATCH]
        col.add(
            ids=[i for i, _ in batch],
            documents=[p.text for _, p in batch],
            embeddings=embed_texts([p.text for _, p in batch]),
            metadatas=[{"source": p.source, "year": p.year, "page": p.page or 0} for _, p in batch],
        )
        log.info("%d / %d", min(start + EMBED_BATCH, len(todo)), len(todo))
    return col.count()


def load_index() -> bool:
    """Ouvre ChromaDB et charge le modèle au démarrage du serveur"""
    try:
        n = collection().count()
    except Exception:
        log.exception("ChromaDB indisponible")
        return False
    if n == 0:
        log.warning("Index RAG vide : lance `uv run python -m sunuchat.knowledge build`")
        return False
    embedding_model()
    log.info("Index RAG prêt : %d passages", n)
    return True


def search(query: str, k: int = 4, max_distance: float = 0.6) -> list[Passage]:
    col = collection()
    if col.count() == 0:
        return []
    res = col.query(
        query_embeddings=embed_texts([query]),
        n_results=k,
        include=["documents", "metadatas", "distances"],
    )
    passages = []
    for text, meta, dist in zip(
        res["documents"][0], res["metadatas"][0], res["distances"][0], strict=True
    ):
        if dist <= max_distance:
            passages.append(
                Passage(text, meta["source"], meta.get("year", ""), meta.get("page") or None)
            )
    return passages


async def asearch(query: str, k: int = 4) -> list[Passage]:
    return await asyncio.to_thread(search, query, k)


def format_passages(passages: list[Passage]) -> str:
    if not passages:
        return "Aucun passage trouvé dans les sources officielles."
    return "\n\n".join(f"[{i}] ({p.citation()})\n{p.text}" for i, p in enumerate(passages, 1))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m sunuchat.knowledge")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="construire l'index RAG")
    b.add_argument("--kb", type=Path, default=None, help="dossier du corpus (défaut : KB_DIR)")
    b.add_argument("--limit", type=int, default=None, help="n'indexer que les N premiers passages")
    b.add_argument("--no-web", action="store_true", help="ne pas télécharger les pages web")
    b.add_argument("--reset", action="store_true", help="vider l'index avant de reconstruire")
    sub.add_parser("list", help="lister les documents gardés du corpus")
    q = sub.add_parser("search", help="tester une recherche")
    q.add_argument("query")
    args = parser.parse_args()

    s = get_settings()
    if args.cmd == "list":
        chunks = s.kb_dir / "chunks.jsonl"
        if chunks.exists():
            for title, n in Counter(p.source for p in read_clean_chunks(chunks)).items():
                print(f"{n:5}  {title}")
        else:
            for path in kb_files(s.kb_dir):
                print(path.name)
    elif args.cmd == "build":
        passages = asyncio.run(collect_passages(args.kb or s.kb_dir, web=not args.no_web))
        n = build_index(passages, args.limit, reset=args.reset)
        print(f"Index prêt : {n} passages dans ChromaDB")
    elif args.cmd == "search":
        for p in search(args.query):
            print(f"--- {p.citation()}\n{p.text[:400]}\n")


if __name__ == "__main__":
    main()
