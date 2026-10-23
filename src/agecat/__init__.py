"""AgeCat core package."""

import logging
from importlib.metadata import PackageNotFoundError, version

from .client import AgeClient
from .errors import (
    AccessDeniedError,
    AgeCatError,
    DownloadCancelled,
    DownloadError,
    FetchError,
    ParseError,
)
from .models import DownloadProgressEvent, DownloadResult, Episode, EpisodeJob, JobReport
from .options import DownloadOptions
from .search import SearchResult
from .service import AgeCatService

__all__ = [
    "AccessDeniedError",
    "AgeCatError",
    "AgeCatService",
    "AgeClient",
    "DownloadCancelled",
    "DownloadError",
    "DownloadOptions",
    "DownloadProgressEvent",
    "DownloadResult",
    "Episode",
    "EpisodeJob",
    "FetchError",
    "JobReport",
    "ParseError",
    "SearchResult",
    "__version__",
]

try:
    # Read from installed metadata so this can never drift from pyproject.
    __version__ = version("agecat")
except PackageNotFoundError:
    __version__ = "0.0.0+source"

logging.getLogger(__name__).addHandler(logging.NullHandler())
