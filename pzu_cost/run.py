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
VAT = float(OPTIONS.get("vat", 0))

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
    r = requests.post(f"{SUPERVISOR_URL}/states/{entity_id}", headers=HEADERS, json=payload, timeout=10)
    r.raise_for_status()


def ha_statistics_sum(entity_id, start_time, end_time):
    url = f"{SUPERVISOR_URL}/history/statistics/period"
    params = {
        "start_time": start_time,
        "end_time": end_time,
        "statistic_ids": entity_id,
        "types": "sum",
    }

    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()

    data = r.json().get(entity_id)
    if not data:
        return None

    return data[0]["sum"]

# ======================================================
# PZU (today, published yesterday)
# ======================================================

def load_pzu_prices_today():
    today = datetime.now().date()
    url = f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/{today.day:02d}/{today.month:02d}/{today.year}/ro"
    print("PZU URL:", url)

    r = requests.get(url, timeout=20)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    prices = {}

    for d in root.iter("Detail"):
        interval = int(d.find("Interval").text)
        price = float(d.find("Price").text) / 1000.0
        prices[interval] = price

    return prices

# ======================================================
# Helpers
# ======================================================

def current_interval():
    now = datetime.now()
    return (now.hour * 60 + now.minute) // 15 + 1


def interval_timestamp(interval, day):
    base = datetime.fromisoformat(day)
    return (base + timedelta(minutes=(interval - 1) * 15)).isoformat()

# ======================================================
# Recompute FULL day at startup
# ======================================================

def recompute_today(prices):
    today = datetime.now().date().isoformat()
    midnight = f"{today}T00:00:00"
    now = datetime.now().isoformat()

    imp_kwh = ha_statistics_sum(IMPORTED_SENSOR, midnight, now) or 0.0
    exp_kwh = ha_statistics_sum(EXPORTED_SENSOR, midnight, now) or 0.0

    cost = 0.0
    value = 0.0

    current_int = current_interval()
    for i in range(1, current_int + 1):
        price = prices.get(i, 0)
        cost += (imp_kwh / current_int) * (price + TOTAL_TARIFF)
        value += (exp_kwh / current_int) * price

    last_imp = ha_get_state(IMPORTED_SENSOR)
    last_exp = ha_get_state(EXPORTED_SENSOR)

    return cost, value, last_imp, last_exp

# ======================================================
# Persistence
# ======================================================

STATE_FILE = "/data/realtime_state.json"

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

# ======================================================
# MAIN
# ======================================================

def main():
    print("Starting REAL-TIME PZU addon (robust mode)")

    prices = load_pzu_prices_today()
    today = datetime.now().date().isoformat()

    import_cost, export_value, last_imp, last_exp = recompute_today(prices)

    state = {
        "day": today,
        "last_import": last_imp,
        "last_export": last_exp,
        "import_cost": import_cost,
        "export_value": export_value,
    }

    save_state(state)

    while True:
        now = datetime.now()
        today = now.date().isoformat()

        # publish PZU sensor
        interval = current_interval()
        price = prices.get(interval, 0)

        attrs = {
            interval_timestamp(i, today): round(p, 4)
            for i, p in prices.items()
        }

        ha_set_state(
            "sensor.pzu_price",
            round(price, 4),
            {
                "unit_of_measurement": "RON/kWh",
                "state_class": "measurement",
                "device_class": "monetary",
                "interval": interval,
                "prices": attrs,
                "source": "OPCOM",
            },
        )

        # read meters
        imp = ha_get_state(IMPORTED_SENSOR)
        exp = ha_get_state(EXPORTED_SENSOR)

        delta_imp = max(0, imp - state["last_import"])
        delta_exp = max(0, exp - state["last_export"])

        state["import_cost"] += delta_imp * (price + TOTAL_TARIFF)
        state["export_value"] += delta_exp * price

        ha_set_state(
            "sensor.pzu_import_cost",
            round(state["import_cost"], 2),
            {"unit_of_measurement": "RON", "state_class": "measurement"},
        )

        ha_set_state(
            "sensor.pzu_export_value",
            round(state["export_value"], 2),
            {"unit_of_measurement": "RON", "state_class": "measurement"},
        )

        state["last_import"] = imp
        state["last_export"] = exp
        save_state(state)

        time.sleep(300)

# ======================================================

if __name__ == "__main__":
    main()
