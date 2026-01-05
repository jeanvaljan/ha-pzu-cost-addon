#!/usr/bin/env python3

import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

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

with open("/data/options.json") as f:
    OPTIONS = json.load(f)

IMPORTED_SENSOR = OPTIONS["imported_sensor"]
EXPORTED_SENSOR = OPTIONS["exported_sensor"]

TARIFF_DISTRIBUTION = float(OPTIONS.get("tariff_distribution", 0))
TARIFF_TRANSPORT = float(OPTIONS.get("tariff_transport", 0))
TARIFF_SYSTEM = float(OPTIONS.get("tariff_system", 0))
TARIFF_COGENERATION = float(OPTIONS.get("tariff_cogeneration", 0))

TOTAL_TARIFF = (
    TARIFF_DISTRIBUTION
    + TARIFF_TRANSPORT
    + TARIFF_SYSTEM
    + TARIFF_COGENERATION
)

# ======================================================
# HA helpers
# ======================================================

def ha_get_state(entity_id):
    r = requests.get(f"{SUPERVISOR_URL}/states/{entity_id}", headers=HEADERS, timeout=10)
    r.raise_for_status()
    return float(r.json()["state"])


def ha_set_state(entity_id, state, attributes=None):
    payload = {"state": state, "attributes": attributes or {}}
    r = requests.post(
        f"{SUPERVISOR_URL}/states/{entity_id}",
        headers=HEADERS,
        json=payload,
        timeout=10,
    )
    r.raise_for_status()


# ======================================================
# Statistics (STATE for total_increasing)
# ======================================================

def ha_statistics_sum_at(statistic_id, start):
    url = f"{SUPERVISOR_URL}/history/statistics/period/{start}"
    params = {
        "statistic_ids": f"energy:{statistic_id}",
        "types": "state",
    }

    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()

    data = r.json().get(f"energy:{statistic_id}")
    if not data:
        return None

    return data[0]["state"]


# ======================================================
# PZU prices (today, published yesterday)
# ======================================================

def load_pzu_prices_for_today():
    today = datetime.now().date()
    url = (
        "https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/"
        f"{today.day:02d}/{today.month:02d}/{today.year}/ro"
    )

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
# Helpers
# ======================================================

def current_interval():
    now = datetime.now()
    return (now.hour * 60 + now.minute) // 15 + 1


def interval_start(interval, day):
    base = datetime.fromisoformat(day)
    return base + timedelta(minutes=(interval - 1) * 15)


# ======================================================
# Recompute FULL DAY at startup
# ======================================================

def recompute_today(prices):
    today = datetime.now().date().isoformat()
    midnight = f"{today}T00:00:00"

    imp_start = ha_statistics_sum_at(IMPORTED_SENSOR, midnight)
    exp_start = ha_statistics_sum_at(EXPORTED_SENSOR, midnight)

    if imp_start is None or exp_start is None:
        print("Statistics not ready yet, skipping recompute")
        return 0.0, 0.0, None, None

    imp_now = ha_get_state(IMPORTED_SENSOR)
    exp_now = ha_get_state(EXPORTED_SENSOR)

    delta_import = max(0, imp_now - imp_start)
    delta_export = max(0, exp_now - exp_start)

    intervals_passed = current_interval()
    avg_price = sum(
        prices.get(i, 0) for i in range(1, intervals_passed + 1)
    ) / max(1, intervals_passed)

    import_cost = delta_import * (avg_price + TOTAL_TARIFF)
    export_value = delta_export * avg_price

    return import_cost, export_value, imp_now, exp_now


# ======================================================
# Persistence
# ======================================================

STATE_FILE = "/data/realtime_state.json"

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ======================================================
# MAIN LOOP
# ======================================================

def main():
    print("Starting REAL-TIME PZU addon (robust mode)")

    prices = load_pzu_prices_for_today()
    state = load_state()

    # 🔁 FULL recompute at startup
    cost, value, last_imp, last_exp = recompute_today(prices)

    today = datetime.now().date().isoformat()

    state = {
        "day": today,
        "last_import": last_imp,
        "last_export": last_exp,
        "import_cost": cost,
        "export_value": value,
    }
    save_state(state)

    while True:
        now = datetime.now()
        today = now.date().isoformat()

        # reset at midnight
        if state["day"] != today:
            state = {
                "day": today,
                "last_import": ha_get_state(IMPORTED_SENSOR),
                "last_export": ha_get_state(EXPORTED_SENSOR),
                "import_cost": 0.0,
                "export_value": 0.0,
            }
            save_state(state)

        imp = ha_get_state(IMPORTED_SENSOR)
        exp = ha_get_state(EXPORTED_SENSOR)

        delta_import = max(0, imp - state["last_import"])
        delta_export = max(0, exp - state["last_export"])

        interval = current_interval()
        pzu_price = prices.get(interval, 0)

        state["import_cost"] += delta_import * (pzu_price + TOTAL_TARIFF)
        state["export_value"] += delta_export * pzu_price

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

        time.sleep(300)


# ======================================================

if __name__ == "__main__":
    main()
