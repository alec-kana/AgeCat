from __future__ import annotations

# AGE moves domains every few months; the "easy" domains 302 to the current one.
DEFAULT_SITE_URL = "https://www.agedm.io"
DOMAIN_RESOLVER_URLS = ("http://www.age.tv", "http://www.agedm.com", "http://agefans.com")
DEFAULT_OUTPUT_DIR_NAME = "age"

# Playlist tab keys in the order sources are tried. The VIP tab serves a
# direct MP4 and is fastest; the m3u8 tabs are third-party HLS mirrors.
DEFAULT_SOURCE_PRIORITY = (
    "xigua",
    "ffm3u8",
    "wjm3u8",
    "hnm3u8",
    "lzm3u8",
    "bfzym3u8",
    "wolong",
)

DEFAULT_CONCURRENCY = 3
DEFAULT_SEGMENT_CONCURRENCY = 4
DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 30.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF = 0.5

DEFAULT_CHUNK_SIZE = 512 * 1024
DEFAULT_MAX_FILENAME_STEM_LENGTH = 180

PROGRESS_TITLE_MAX_LENGTH = 42
PLAIN_PROGRESS_ENV_VAR = "AGECAT_PLAIN_PROGRESS"
FFMPEG_ENV_VAR = "AGECAT_FFMPEG"
KEEP_PARTIAL_ENV_VAR = "AGECAT_KEEP_PARTIAL"
# A partial download in a folder the current run did not use is only swept
# once it has been untouched this long: well past the retry budget, so a
# file another run is still recovering is never deleted from under it.
STALE_PARTIAL_AGE = 30 * 60.0

# Pacing. The host interval is what prevents simultaneous requests; the
# per-episode delays only break up cadence, so they stay short.
DEFAULT_MIN_DELAY = 0.5
DEFAULT_MAX_DELAY = 2.0
DEFAULT_STAGGER = 2.0
DEFAULT_HOST_INTERVAL = 0.5
DEFAULT_MAX_BACKOFF = 30.0
MAX_RETRY_AFTER = 120.0

# Recovery: bounded re-resolution of expired credentials, then give up.
DEFAULT_RESOLVE_ATTEMPTS = 3
DEFAULT_RETRY_BUDGET = 600.0
DEFAULT_BLOCKED_BACKOFF = 15.0
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 5
DEFAULT_CIRCUIT_BREAKER_COOLDOWN = 60.0

FORBIDDEN_STATUS_CODE = 403
RETRY_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
