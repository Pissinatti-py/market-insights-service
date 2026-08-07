from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

# The only collectors that exist — a typo'd key would otherwise silently no-op.
_VALID_SOURCES = {"github", "packages", "articles"}


def _check_source_keys(v: dict | None) -> dict | None:
    if v is not None:
        unknown = set(v) - _VALID_SOURCES
        if unknown:
            raise ValueError(f"unknown source(s) {sorted(unknown)}; valid: {sorted(_VALID_SOURCES)}")
    return v


class PreferenceUpdate(BaseModel):
    """Partial update of the singleton preferences row."""

    stacks: list[str] | None = Field(None, description="Languages/stacks — filter GitHub trending (language:x)")
    areas: list[str] | None = Field(None, description="Topic areas — Dev.to/HN/Medium article tags")
    keywords: list[str] | None = Field(
        None, description="Search terms — GitHub search + relevance scoring, and article tags"
    )
    monitored_libraries: list[str] | None = Field(
        None, description='"ecosystem:name" entries (pypi/npm) polled for new releases, e.g. "pypi:fastapi"'
    )
    enabled_sources: dict | None = Field(None, description="Per-collector on/off: {github|packages|articles: bool}")

    _sources_known = field_validator("enabled_sources")(_check_source_keys)


class PreferenceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stacks: list[str]
    areas: list[str]
    keywords: list[str]
    monitored_libraries: list[str]
    enabled_sources: dict
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def profile_empty(self) -> bool:
        """Nothing to target — collectors will run broad/global queries."""
        return not (self.stacks or self.areas or self.keywords)


class KeywordSuggestion(BaseModel):
    """
    A candidate search term mined from review decisions (``GET /api/config/keyword-suggestions``).

    ``net`` ranks them: a tag that shows up as often in rejections as in approvals
    discriminates nothing and is not worth searching for.
    """

    keyword: str
    approved_count: int
    rejected_count: int
    net: int


class SourcesConfig(BaseModel):
    """Configure which collectors are enabled (``POST /api/config/sources``)."""

    enabled_sources: dict = Field(description="Per-collector on/off: {github|packages|articles: bool}")

    _sources_known = field_validator("enabled_sources")(_check_source_keys)
