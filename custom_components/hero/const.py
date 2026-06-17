"""Constants for the Hero integration."""

from datetime import timedelta

DOMAIN = "hero"

API_BASE = "https://api4.linc-ed.com"
AUTH_BASE = "https://id.linc-ed.com"
APP_ORIGIN = "https://our.linc-ed.com"
APP_REFERER = "https://our.linc-ed.com/"
CLIENT_ID = "57e6f49e-b45b-409a-be40-d42dc77bc09a"

DEFAULT_TENANT_ID = "c083dae4-75e7-4b68-bcb0-c485cdb84770"
DEFAULT_TIMEZONE = "Pacific/Auckland"

CONF_TENANT_ID = "tenant_id"
CONF_TIMEZONE = "timezone"

PLATFORMS = ["calendar"]
SCAN_INTERVAL = timedelta(hours=6)


CONF_HIDE_WEEK_EVENTS = "hide_week_events"
CONF_EXCLUDED_WORDS = "excluded_words"
