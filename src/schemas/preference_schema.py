from datetime import datetime

from pydantic import BaseModel, ConfigDict, computed_field


class PreferenceUpdate(BaseModel):
    """Partial update of the singleton preferences row."""

    stacks: list[str] | None = None
    areas: list[str] | None = None
    keywords: list[str] | None = None
    monitored_libraries: list[str] | None = None
    enabled_sources: dict | None = None


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


class SourcesConfig(BaseModel):
    """Configure which collectors are enabled (``POST /api/config/sources``)."""

    enabled_sources: dict
