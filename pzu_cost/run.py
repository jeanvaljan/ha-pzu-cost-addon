#!/usr/bin/env python3

import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ======================================================
# Supervisor API
# ======================================================

SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN")

if not SUPERVISOR_TOKEN:
    with open("/var/run/secrets/supervisor_token") as f:
        SUPERVISOR_TOKEN = f.read().strip()

SUPERVISOR_URL = "http://supervisor/core/api"

HEADERS = {
    "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
    "Content-Type": "application/json",
}

# ======================================================
# Load options
# ======================================================

OPTIONS = {}
if os.path.exists("/data/options.json"):
    with open("/data/options.json") as f:
        OPTIONS = json.load(f)

IMPORTED_SENSOR = OPTIONS["imported_sensor"]
EXPORTED_SENSOR = OPTIONS["exported_sensor"]

TARIFF_DISTRIBUTION = float(OPTIONS.get("tariff_distribution", 0))
TARIFF_TRANSPORT = float(OPTIONS.get("tariff_transport", 0))
TARIFF_OTHER = float(OPTIONS.get("tariff_other", 0))

TOTAL_TARIFF = TARIFF_DISTRIBUTION + TARIFF_TRANSPORT + TARIFF_OTHER

# ======================================================
# HA helpers
# ======================================================

def ha_get_state(entity_id):
    r = requests.get(f"{SUPERVISOR_URL}/states/{entity_id}", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return float(r.json()["state"])


def ha_set_state(entity_id, state, attributes=None):
    payload = {"state": state, "attributes": attributes or {}}
    r = requests.post(f"{SUPERVISOR_URL}/states/{entity_id}", headers=HEADERS, json=payload)
    print("request:")
    print(r.request.url)
    print(r.request.body)
    print(r.request.headers)
    r.raise_for_status()

# ======================================================
# PZU prices (for TODAY, published yesterday)
# ======================================================

def load_pzu_prices_for_today():
    today = datetime.now().date()
    url = f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/{today.day:02d}/{today.month:02d}/{today.year}/ro"
    print("PZU URL:", url)

    r = requests.get(url, timeout=20)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    prices = {}

    for d in root.iter("Detail"):
        interval = int(d.find("Interval").text)
        price_kwh = float(d.find("Price").text) / 1000.0
        prices[interval] = price_kwh

    return prices

# ======================================================
# Interval helper
# ======================================================

def current_interval():
    now = datetime.now()
    return (now.hour * 60 + now.minute) // 15 + 1

# ======================================================
# Persistence
# ======================================================

STATE_FILE = "/data/realtime_state.json"

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"day": None, "last_import": None, "last_export": None, "import_cost": 0.0, "export_value": 0.0}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ======================================================
# MAIN LOOP
# ======================================================

def main():
    print("Starting REAL-TIME PZU addon")

    prices = load_pzu_prices_for_today()
    state = load_state()

    while True:
        now = datetime.now()
        today = str(now.date())

        # Reset at midnight
        if state["day"] != today:
            print("New day detected, resetting counters")
            state = {
                "day": today,
                "last_import": ha_get_state(IMPORTED_SENSOR),
                "last_export": ha_get_state(EXPORTED_SENSOR),
                "import_cost": 0.0,
                "export_value": 0.0,
            }
            save_state(state)

        # Read meters
        imp = ha_get_state(IMPORTED_SENSOR)
        exp = ha_get_state(EXPORTED_SENSOR)
        print("Index import:", imp)
        print("Index export:", exp)

        if state["last_import"] is not None:
            delta_import = max(0, imp - state["last_import"])
            delta_export = max(0, exp - state["last_export"])
            print("delta_import:", delta_import)
            #delta_export = abs(delta_export)
            print("delta_export:", delta_export)

            interval = current_interval()
            print("Interval:", interval)
            pzu_price = prices.get(interval, 0)

            state["import_cost"] += delta_import * (pzu_price + TOTAL_TARIFF)
            state["export_value"] += delta_export * pzu_price
            print("import_cost:", state["import_cost"])
            print("export_value:", state["export_value"])

            ha_set_state(
                "sensor.pzu_import_cost",
                round(state["import_cost"], 2),
                {
                    "unit_of_measurement": "RON",
                    "device_class": "monetary",
                    "state_class": "measurement",
                },
            )

            ha_set_state(
                "sensor.pzu_export_value",
                round(state["export_value"], 2),
                {
                    "unit_of_measurement": "RON",
                    "device_class": "monetary",
                    "state_class": "measurement",
                },
            )

        state["last_import"] = imp
        state["last_export"] = exp
        save_state(state)

        time.sleep(300)  # 5 minutes

# ======================================================

if __name__ == "__main__":
    main()
