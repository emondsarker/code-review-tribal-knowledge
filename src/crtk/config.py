from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_SEARCH_PATHS = [
    lambda: os.environ.get("CRTK_CONFIG"),
    lambda: str(Path.home() / ".config" / "crtk" / "crtk.toml"),
    lambda: str(Path.cwd() / "crtk.toml"),
]


@dataclass
class FetchConfig:
    page_size: int = 100
    max_retries: int = 5
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    inter_request_delay: float = 0.5


@dataclass
class SearchConfig:
    fts_weight: float = 0.4
    vector_weight: float = 0.6
    default_limit: int = 15


@dataclass
class EmbeddingsConfig:
    model: str = "nomic-ai/nomic-embed-code"


@dataclass
class TaggingConfig:
    auto_tag_on_fetch: bool = True


@dataclass
class SynthesisConfig:
    mode: str = "template"
    max_comments: int = 20


@dataclass
class CrtkConfig:
    db_path: str = "~/.local/share/crtk/crtk.db"
    log_level: str = "INFO"
    repos: list[str] = field(default_factory=list)
    fetch: FetchConfig = field(default_factory=FetchConfig)
    search: SearchConfig = field(default_factory=SearchConfig)
    embeddings: EmbeddingsConfig = field(default_factory=EmbeddingsConfig)
    tagging: TaggingConfig = field(default_factory=TaggingConfig)
    synthesis: SynthesisConfig = field(default_factory=SynthesisConfig)

    @property
    def resolved_db_path(self) -> Path:
        return Path(self.db_path).expanduser()


def load_config(config_path: str | None = None) -> CrtkConfig:
    """Load config from file. Searches CRTK_CONFIG env, ~/.config/crtk/, then cwd."""
    paths_to_try: list[str] = []
    if config_path:
        paths_to_try.append(config_path)
    else:
        for path_fn in _SEARCH_PATHS:
            p = path_fn()
            if p:
                paths_to_try.append(p)

    for path_str in paths_to_try:
        path = Path(path_str)
        if path.is_file():
            logger.info("Loading config from %s", path)
            with open(path, "rb") as f:
                raw = tomllib.load(f)
            return _parse_config(raw)

    logger.warning("No config file found, using defaults")
    return CrtkConfig()


def _parse_config(raw: dict) -> CrtkConfig:
    general = raw.get("general", {})
    repos_section = raw.get("repos", {})
    fetch_section = raw.get("fetch", {})
    search_section = raw.get("search", {})
    embeddings_section = raw.get("embeddings", {})
    tagging_section = raw.get("tagging", {})
    synthesis_section = raw.get("synthesis", {})

    return CrtkConfig(
        db_path=general.get("db_path", CrtkConfig.db_path),
        log_level=general.get("log_level", CrtkConfig.log_level),
        repos=repos_section.get("list", []),
        fetch=FetchConfig(**{k: v for k, v in fetch_section.items() if hasattr(FetchConfig, k)}),
        search=SearchConfig(**{k: v for k, v in search_section.items() if hasattr(SearchConfig, k)}),
        embeddings=EmbeddingsConfig(**{k: v for k, v in embeddings_section.items() if hasattr(EmbeddingsConfig, k)}),
        tagging=TaggingConfig(**{k: v for k, v in tagging_section.items() if hasattr(TaggingConfig, k)}),
        synthesis=SynthesisConfig(**{k: v for k, v in synthesis_section.items() if hasattr(SynthesisConfig, k)}),
    )
