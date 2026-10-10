import hashlib
import json

import numpy as np
import pytest

from sunuchat import knowledge
from sunuchat.config import get_settings


class FakeModel:
    """Remplace le vrai modèle (2 Go) : vecteurs construits à partir des mots du texte."""

    def encode(self, texts, **kwargs):
        vecs = []
        for t in texts:
            v = np.zeros(64, dtype=np.float32)
            for word in t.lower().split():
                v[int(hashlib.md5(word.strip(".,?").encode()).hexdigest(), 16) % 64] += 1
            vecs.append(v / (np.linalg.norm(v) or 1))
        return np.array(vecs)


@pytest.fixture
def rag(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "data_dir", tmp_path)
    monkeypatch.setattr(knowledge, "_model", FakeModel())
    monkeypatch.setattr(knowledge, "_collection", None)
    yield knowledge
    knowledge._collection = None


def test_build_and_search(rag):
    P = rag.Passage
    passages = [
        P("lavez la moustiquaire avec du savon", "Guide PNLP", "2024", 12),
        P("le vaccin hexavalent est donné à six semaines", "UNICEF", "2025"),
        P("la dengue est transmise par le moustique aedes", "OMS"),
    ]
    assert rag.build_index(passages) == 3
    # Relancer ne duplique rien.
    assert rag.build_index(passages) == 3
    best = rag.search("comment laver la moustiquaire avec du savon", k=1, max_distance=1.0)
    assert best[0].source == "Guide PNLP"
    assert best[0].page == 12


def test_read_clean_chunks(tmp_path):
    rows = [
        {
            "titre": "Guide national de diagnostic biologique du paludisme",
            "source": "PNLP",
            "annee": "2016",
            "page": 4,
            "texte": "La goutte épaisse reste l'examen de référence.",
        },
        {
            "titre": "Bulletin épidémiologique 2021",
            "source": "MSAS",
            "annee": "2021",
            "page": None,
            "texte": "Incidence par district.",
        },
        {
            "titre": "La dengue dans les départements français d'Amérique",
            "source": "IRD",
            "annee": "2019",
            "page": 3,
            "texte": "Aedes aegypti aux Antilles.",
        },
    ]
    path = tmp_path / "chunks.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
    passages = knowledge.read_clean_chunks(path)
    assert len(passages) == 1  # bulletin et document hors Sénégal écartés
    p = passages[0]
    assert p.source == "Guide national de diagnostic biologique du paludisme (PNLP)"
    assert (p.year, p.page) == ("2016", 4)
