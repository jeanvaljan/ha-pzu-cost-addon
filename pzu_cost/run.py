#!/usr/bin/env python3

import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime
import pytz

# ======================================================
# CONFIG
# ======================================================

OPTIONS = json.load(open("/data/options.json"))

IMPORTED_SENSOR = OPTIONS["imported_sensor"]
EXPORTED_SENSOR = OPTIONS["exported_sensor"]

TARIFF_DISTRIBUTION = float(OPTIONS.get("tariff_distribution", 0))
TARIFF_TRANSPORT = float(OPTIONS.get("tariff_transport", 0))
TARIFF_OTHER = float(OPTIONS.get("tariff_other", 0))

TIMEZONE = pytz.timezone("Europe/Bucharest")

STATE_FILE = "/data/last_energy.json"
DAY_FILE = "/data/current_day.json"

# ======================================================
# SUPERVISOR API
# ======================================================

SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN")
if not SUPERVISOR_TOKEN:
    with open("/var/run/secrets/supervisor_token") as f:
        SUPERVISOR_TOKEN = f.read().strip()

HEADERS = {
    "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
    "Content-Type": "application/json",
}

BASE_URL = "http://supervisor/core/api"

# ======================================================
# HELPERS
# ======================================================

def ha_get_state(entity):
    r = requests.get(f"{BASE_URL}/states/{entity}", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return float(r.json()["state"])


def ha_set_state(entity, state, attrs):
    requests.post(
        f"{BASE_URL}/states/{entity}",
        headers=HEADERS,
        json={"state": round(state, 4), "attributes": attrs},
        timeout=10,
    ).raise_for_status()


def load_json(path, default):
    if os.path.exists(path):
        return json.load(open(path))
    return default


def save_json(path, data):
    json.dump(data, open(path, "w"))


# ======================================================
# PZU
# ======================================================

def load_pzu_prices(today):
    url = f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/{today.day:02d}/{today.month:02d}/{today.year}/ro"
    r = requests.get(url, timeout=20)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    prices = {}

    for d in root.iter("Detail"):
        interval = int(d.find("Interval").text)
        price = float(d.find("Price").text) / 1000.0
        prices[interval] = price

    return prices


def current_interval(now):
    return now.hour * 4 + now.minute // 15 + 1


# ======================================================
# MAIN LOOP
# ======================================================

def main():
    print("PZU REAL-TIME addon started")

    pzu_cache = {}
    current_day = None

    while True:
        now = datetime.now(TIMEZONE)
        today = now.date()

        # ---- Zi noua → reset ----
        if current_day != today:
            print("New day detected, resetting totals")
            current_day = today
            pzu_cache = load_pzu_prices(today)

            save_json(DAY_FILE, {
                "date": str(today),
                "import_cost": 0.0,
                "export_value": 0.0
            })

            save_json(STATE_FILE, {
                "import": ha_get_state(IMPORTED_SENSOR),
                "export": ha_get_state(EXPORTED_SENSOR)
            })

        # ---- Citire energie ----
        last = load_json(STATE_FILE, {})
        curr_import = ha_get_state(IMPORTED_SENSOR)
        curr_export = ha_get_state(EXPORTED_SENSOR)

        delta_import = max(0, curr_import - last.get("import", curr_import))
        delta_export = max(0, curr_export - last.get("export", curr_export))

        save_json(STATE_FILE, {
            "import": curr_import,
            "export": curr_export
        })

        if delta_import == 0 and delta_export == 0:
            time.sleep(300)
            continue

        # ---- Interval curent ----
        interval = current_interval(now)
        price = pzu_cache.get(interval)

        if price is None:
            time.sleep(300)
            continue

        tariff = TARIFF_DISTRIBUTION + TARIFF_TRANSPORT + TARIFF_OTHER

        day = load_json(DAY_FILE, {})
        day["import_cost"] += delta_import * (price + tariff)
        day["export_value"] += delta_export * price

        save_json(DAY_FILE, day)

        # ---- Update senzori HA ----
        ha_set_state(
            "sensor.pzu_import_cost",
            day["import_cost"],
            {
                "unit_of_measurement": "RON",
                "device_class": "monetary",
                "state_class": "measurement",
            }
        )

        ha_set_state(
            "sensor.pzu_export_value",
            day["export_value"],
            {
                "unit_of_measurement": "RON",
                "device_class": "monetary",
                "state_class": "measurement",
            }
        )

        print(
            f"[{now.strftime('%H:%M')}] ΔImport={delta_import:.4f} kWh "
            f"ΔExport={delta_export:.4f} kWh Interval={interval}"
        )

        time.sleep(300)


# ======================================================
if __name__ == "__main__":
    main()
