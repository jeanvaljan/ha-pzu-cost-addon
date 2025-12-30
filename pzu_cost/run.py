import requests
import xml.etree.ElementTree as ET
import json
from datetime import datetime, timedelta, timezone

# =========================
# CONSTANTE HA
# =========================

HA_URL = "http://supervisor/core"

HEADERS = {
    "Authorization": f"Bearer {open('/var/run/secrets/supervisor_token').read().strip()}",
    "Content-Type": "application/json",
}

# =========================
# LOAD OPTIONS
# =========================

def load_options():
    try:
        with open("/data/options.json", "r") as f:
            return json.load(f)
    except Exception as e:
        print("ERROR loading options.json:", e)
        return {}

OPTIONS = load_options()

IMPORTED_SENSOR = OPTIONS.get("imported_sensor")
EXPORTED_SENSOR = OPTIONS.get("exported_sensor")

TARIFF_DISTRIBUTION = float(OPTIONS.get("tariff_distribution", 0.0))
TARIFF_TRANSPORT = float(OPTIONS.get("tariff_transport", 0.0))
TARIFF_OTHER = float(OPTIONS.get("tariff_other", 0.0))

# =========================
# HA HELPERS
# =========================

def ha_get_state(entity_id):
    if not entity_id:
        return 0.0
