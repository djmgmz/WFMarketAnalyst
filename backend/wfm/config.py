"""Runtime settings, overridable through environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value else default


@dataclass(frozen=True)
class Settings:
    db_path: Path = field(
        default_factory=lambda: Path(_env("WFM_DB_PATH", str(PROJECT_ROOT / "data" / "wfm.db")))
    )
    api_base: str = _env("WFM_API_BASE", "https://api.warframe.market")
    ws_url: str = _env("WFM_WS_URL", "wss://ws.warframe.market/socket")
    platform: str = _env("WFM_PLATFORM", "pc")
    crossplay: bool = _env("WFM_CROSSPLAY", "true").lower() == "true"
    language: str = "en"
    # The public limit is 3 requests per second: stay comfortably below it, and never let WFM_RPS
    # raise the rate past it. Enforced across all processes by client.SharedRateLimiter.
    requests_per_second: float = min(float(_env("WFM_RPS", "2.5")), 3.0)
    # The API rules ask every client to identify itself. Set WFM_CONTACT to a
    # repository URL or email so the warframe.market team can reach you.
    contact: str = _env("WFM_CONTACT", "local portfolio project")
    top_cache_seconds: int = 60

    @property
    def rate_limit_path(self) -> Path:
        """State file for the rate limiter shared by every wfm process."""
        return self.db_path.parent / "ratelimit.db"

    @property
    def images_dir(self) -> Path:
        """Item images downloaded by the item-images job (never committed: data/ is ignored)."""
        return self.db_path.parent / "images"

    @property
    def user_agent(self) -> str:
        return f"WFMarketAnalyst/{__version__} (+{self.contact})"


settings = Settings()
