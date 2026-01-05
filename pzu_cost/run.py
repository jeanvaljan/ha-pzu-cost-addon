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
    r = requests.get(
        f"{SUPERVISOR_URL}/states/{entity_id}",
        headers=HEADERS,
        timeout=10,
    )
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


def ha_statistics_at_midnight(entity_id, day):
    start = f"{day}T00:00:00"
    url = f"{SUPERVISOR_URL}/history/statistics/period/{start}?statistic_ids={entity_id}"
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    if not data or entity_id not in data:
        raise RuntimeError(f"No statistics for {entity_id}")
    return float(data[entity_id][0]["sum"])

# ======================================================
# PZU prices (TODAY)
# ======================================================

def load_pzu_prices_for_today():
    today = datetime.now().date()
    url = (
        f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/"
        f"{today.day:02d}/{today.month:02d}/{today.year}/ro"
    )
    r = requests.get(url, timeout=30)
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
# RECOMPUTE FROM MIDNIGHT (CRITICAL)
# ======================================================

def recompute_today(prices):
    today = str(datetime.now().date())

    import_start = ha_statistics_at_midnight(IMPORTED_SENSOR, today)
    export_start = ha_statistics_at_midnight(EXPORTED_SENSOR, today)

    import_now = ha_get_state(IMPORTED_SENSOR)
    export_now = ha_get_state(EXPORTED_SENSOR)

    import_total = import_now - import_start
    export_total = export_now - export_start

    cost = 0.0
    value = 0.0

    intervals = current_interval()
    if intervals <= 0:
        return cost, value, import_now, export_now

    import_per_interval = import_total / intervals
    export_per_interval = export_total / intervals

    for i in range(1, intervals + 1):
        pzu = prices.get(i, 0)
        cost += import_per_interval * (pzu + TOTAL_TARIFF)
        value += export_per_interval * pzu

    return cost, value, import_now, export_now

# ======================================================
# MAIN
# ======================================================

def main():
    print("Starting REAL-TIME PZU addon (robust mode)")

    prices = load_pzu_prices_for_today()
    state = load_state()
    today = str(datetime.now().date())

    # ===== RECOMPUTE AT STARTUP =====
    cost, value, last_imp, last_exp = recompute_today(prices)

    state = {
        "day": today,
        "last_import": last_imp,
        "last_export": last_exp,
        "import_cost": cost,
        "export_value": value,
    }
    save_state(state)

    print("Recomputed at startup:")
    print(" Import cost:", cost)
    print(" Export value:", value)

    # ===== MAIN LOOP =====
    while True:
        now = datetime.now()
        today = str(now.date())

        if state["day"] != today:
            prices = load_pzu_prices_for_today()
            cost, value, last_imp, last_exp = recompute_today(prices)
            state = {
                "day": today,
                "last_import": last_imp,
                "last_export": last_exp,
                "import_cost": cost,
                "export_value": value,
            }

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
