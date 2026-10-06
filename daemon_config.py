"""Process-level configuration read once from the environment.

Single concept: the composition root's env-driven constants -- the address
and port the HTTP server listens on (loopback by default, because the API has
no authentication) and the application version. The optional shared secret
(``API_TOKEN``) deliberately stays in the composition root itself: tests patch
``displayd.API_TOKEN`` directly, so that name is a documented test hook rather
than process config.
"""

import os

# The API is unauthenticated, so the default is loopback only: reaching it from
# another machine is a deliberate choice (--bind or DISPLAYD_BIND), and should be
# paired with a host firewall or a private network such as a VPN or tailnet.
PORT = int(os.environ.get("DISPLAYD_PORT", "8980"))
BIND = os.environ.get("DISPLAYD_BIND", "127.0.0.1")

# Application version: the single source of truth for displayd's semver.
# It lives here -- not in pyproject.toml -- because the daemon ships as a
# plain script (rsync + systemd, never pip-installed) and still supports
# Python 3.8+, so it cannot rely on importlib.metadata or tomllib to read
# a [project] table. Bump per CHANGELOG.md's convention on every change;
# the daemon reports it via GET /version, GET /state's "version" key,
# and the startup log line in main().
APP_VERSION = "0.8.0"
