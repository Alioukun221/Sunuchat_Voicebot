"""Mémoire par numéro WhatsApp.

- Sessions (conversation du jour) : DatabaseSessionService en SQLite, une session par numéro
  et par jour. Le profil (`user:langue`, `user:localite`, `user:enfants`) est partagé entre
  toutes les sessions d'un même numéro grâce au préfixe `user:`.
- Mémoire longue : SqliteMemoryService. Chaque conversation y est ajoutée après chaque tour,
  et l'agent la retrouve avec l'outil `preload_memory`.
"""

from datetime import date
from functools import lru_cache

from google.adk.memory import SqliteMemoryService
from google.adk.sessions import DatabaseSessionService, Session

from sunuchat.config import get_settings

APP_NAME = "sunuchat"


@lru_cache
def session_service() -> DatabaseSessionService:
    db = get_settings().data_dir / "sessions.db"
    return DatabaseSessionService(db_url=f"sqlite+aiosqlite:///{db}")


@lru_cache
def memory_service() -> SqliteMemoryService:
    return SqliteMemoryService(get_settings().data_dir / "memory.db")


def session_id_for(user_id: str, day: date | None = None) -> str:
    return f"{user_id}-{(day or date.today()).isoformat()}"


async def get_or_create_session(user_id: str) -> Session:
    service = session_service()
    sid = session_id_for(user_id)
    session = await service.get_session(app_name=APP_NAME, user_id=user_id, session_id=sid)
    if session is None:
        session = await service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=sid, state={"user:numero": user_id}
        )
    return session


async def get_profile(user_id: str) -> dict:
    session = await get_or_create_session(user_id)
    return {k.removeprefix("user:"): v for k, v in session.state.items() if k.startswith("user:")}


async def update_profile(user_id: str, **values) -> None:
    """Enregistre des infos durables (langue choisie, inscription…) hors d'un tour d'agent."""
    from google.adk.events import Event, EventActions

    session = await get_or_create_session(user_id)
    delta = {f"user:{k}": v for k, v in values.items()}
    event = Event(author=APP_NAME, actions=EventActions(state_delta=delta))
    await session_service().append_event(session, event)
