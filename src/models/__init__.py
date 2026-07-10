"""Model registry — importing this module registers every mapper with Base.metadata."""

from src.models.article import Article  # noqa
from src.models.curation import Curation, CurationItemType, CurationStatus  # noqa
from src.models.library import Library, LibraryRelease, PackageEcosystem  # noqa
from src.models.preference import Preference  # noqa
from src.models.repository import Repository, RepositorySnapshot  # noqa
from src.models.task_run import TaskRun  # noqa
