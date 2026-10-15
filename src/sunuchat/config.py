"""Variables d'environnement, lues depuis le fichier .env à la racine du projet."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    # env_ignore_empty : une variable vide dans le terminal (ex. GOOGLE_API_KEY=)
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore", env_ignore_empty=True)

    # WhatsApp Cloud API (Meta)
    whatsapp_access_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_pin: str = ""
    graph_api_version: str = "v25.0"

    # Gemini (Google ADK)
    google_api_key: str = ""
    google_genai_use_vertexai: bool = False
    # Modèle de l'agent santé. Avec la facturation activée : gemini-flash-latest.
    agent_model: str = "gemini-flash-lite-latest"
    # Modèle rapide : détection de langue et traduction.
    fast_model: str = "gemini-flash-lite-latest"
    # Modèle de traduction (wolof, pulaar). Vide : FAST_MODEL. gemini-flash-latest écrit un
    # wolof plus juste que la version lite, pour un coût un peu plus élevé.
    translation_model: str = ""
    # Modèles de secours, essayés dans l'ordre quand un modèle refuse (quota gratuit épuisé,
    # surcharge…) : chaque modèle a son propre quota, séparés par des virgules.
    fallback_models: str = "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-flash-lite-latest"
    # Embeddings du RAG : modèle local (sentence-transformers, ~3 Go de mémoire) ou
    # « gemini-embedding-001 » (API Gemini, pour un serveur à 1 Go). Changer : build --reset.
    embedding_model: str = "BAAI/bge-m3"
    # "mps" (GPU du Mac), "cpu" ou "cuda". Vide : choix automatique.
    embedding_device: str = ""

    # Google Maps (MCP Grounding Lite) pour trouver les postes de santé. Facultatif.
    google_maps_api_key: str = ""

    # Voix wolof / pulaar (API Kiriku, compatible OpenAI)
    speech_api_base_url: str = "https://14hyb7tjwzuh9q-8000.proxy.runpod.net/v1"
    speech_api_key: str = ""
    default_language: str = "wolof"

    # Fichiers locaux
    data_dir: Path = ROOT / "data"
    # Corpus nettoyé (chunks.jsonl), produit par scripts/nettoyer_kb.py.
    kb_dir: Path = ROOT / "data" / "kb"

    # Numéro d'urgence affiché en cas de signe de gravité
    samu_number: str = "1515"

    # Modèles de messages approuvés par Meta (WhatsApp Manager → Modèles de message), pour
    # écrire hors de la fenêtre de 24 h. Vides : rappels en texte simple, pas d'alertes.
    template_rappel_vaccin: str = ""  # corps avec 3 variables : âge, date, vaccins
    template_alerte_saison: str = ""  # sans variable, en-tête image facultatif
    # Alerte de la veille sanitaire : {{1}} sujet, {{2}} source, bouton de réponse « Écouter ».
    template_alerte_sante: str = ""
    template_language: str = "fr"

    # Veille sanitaire (veille.py) : sources fiables lues toutes les N minutes, alertes et
    # conseils envoyés d'eux-mêmes aux inscrits. 360 = 6 h ; 5 pour les essais.
    # Désactivée par défaut : rien ne part tant que VEILLE_ACTIVE=true n'est pas dans le .env.
    veille_active: bool = False
    veille_intervalle_min: int = 360
    # Alertes par personne et par semaine (hors gravité 3, qui passe toujours).
    veille_max_par_semaine: int = 3
    # Numéros séparés par des virgules : si rempli, les alertes ne partent qu'à eux (essais).
    veille_test_numeros: str = ""

    def models_after(self, model: str) -> list[str]:
        """Modèles de secours à essayer quand `model` refuse."""
        names = [m.strip() for m in self.fallback_models.split(",") if m.strip()]
        return [m for m in dict.fromkeys(names) if m != model]

    @property
    def graph_url(self) -> str:
        return f"https://graph.facebook.com/{self.graph_api_version}"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings
