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
    r = requests.get(
        f"{SUPERVISOR_URL}/states/{entity_id}",
        headers=HEADERS,
        timeout=10,
    )
    r.raise_for_status()
    return float(r.json()["state"])


def ha_set_state(entity_id, state, attributes=None):
    payload = {
        "state": state,
        "attributes": attributes or {},
    }
    r = requests.post(
        f"{SUPERVISOR_URL}/states/{entity_id}",
        headers=HEADERS,
        json=payload,
        timeout=10,
    )
    r.raise_for_status()


# ======================================================
# History helper (state at midnight)
# ======================================================

def get_state_at_midnight(entity_id, day):
    start = f"{day}T00:00:00"
    end = f"{day}T00:05:00"

    url = f"{SUPERVISOR_URL}/history/period/{start}"
    params = {
        "end_time": end,
        "filter_entity_id": entity_id,
        "minimal_response": "1",
    }

    r = requests.get(url, headers=HEADERS, params=params, timeout=20)
    r.raise_for_status()

    data = r.json()
    if not data or not data[0]:
        return None

    return float(data[0][0]["state"])


# ======================================================
# PZU prices (TODAY, published yesterday)
# ======================================================

def load_pzu_prices_for_today():
    today = datetime.now().date()
    url = (
        f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/"
        f"{today.day:02d}/{today.month:02d}/{today.year}/ro"
    )
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
# Interval helpers
# ======================================================

def current_interval():
    now = datetime.now()
    return (now.hour * 60 + now.minute) // 15 + 1


def interval_to_timestamp(interval, day):
    base = datetime.fromisoformat(day)
    minutes = (interval - 1) * 15
    return (base + timedelta(minutes=minutes)).isoformat()


# ======================================================
# Persistence
# ======================================================

STATE_FILE = "/data/realtime_state.json"


def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "day": None,
            "last_import": None,
            "last_export": None,
            "import_cost": 0.0,
            "export_value": 0.0,
        }
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ======================================================
# Recompute today (robust restart logic)
# ======================================================

def recompute_today(prices):
    today = datetime.now().date().isoformat()

    imp_start = get_state_at_midnight(IMPORTED_SENSOR, today)
    exp_start = get_state_at_midnight(EXPORTED_SENSOR, today)

    imp_now = ha_get_state(IMPORTED_SENSOR)
    exp_now = ha_get_state(EXPORTED_SENSOR)

    if imp_start is None or exp_start is None:
        print("No midnight history found, skipping recompute")
        return 0.0, 0.0, imp_now, exp_now

    imp_kwh = max(0, imp_now - imp_start)
    exp_kwh = max(0, exp_now - exp_start)

    cost = 0.0
    value = 0.0

    current_int = current_interval()

    for i in range(1, current_int + 1):
        price = prices.get(i, 0)
        #cost += (imp_kwh / current_int) * (price + TOTAL_TARIFF)
        #value += (exp_kwh / current_int) * price
        cost += imp_kwh * (price + TOTAL_TARIFF)
        value += exp_kwh * price

    return cost, value, imp_now, exp_now


# ======================================================
# MAIN LOOP
# ======================================================

def main():
    print("Starting REAL-TIME PZU addon (robust mode)")

    prices = load_pzu_prices_for_today()
    state = load_state()

    # Recompute on startup
    import_cost, export_value, last_imp, last_exp = recompute_today(prices)

    state.update(
        {
            "day": str(datetime.now().date()),
            "last_import": last_imp,
            "last_export": last_exp,
            "import_cost": import_cost,
            "export_value": export_value,
        }
    )
    save_state(state)

    while True:
        now = datetime.now()
        today = str(now.date())

        # Publish PZU sensor
        prices_attr = {}
        for interval, price in prices.items():
            ts = interval_to_timestamp(interval, today)
            prices_attr[ts] = round(price, 4)

        current_int = current_interval()
        current_price = prices.get(current_int, 0)

        ha_set_state(
            "sensor.pzu_price",
            round(current_price, 4),
            {
                "unit_of_measurement": "RON/kWh",
                "device_class": "monetary",
                "state_class": "measurement",
                "prices": prices_attr,
                "interval": current_int,
                "source": "OPCOM",
            },
        )

        # New day reset
        if state["day"] != today:
            state = {
                "day": today,
                "last_import": ha_get_state(IMPORTED_SENSOR),
                "last_export": ha_get_state(EXPORTED_SENSOR),
                "import_cost": 0.0,
                "export_value": 0.0,
            }

        imp = ha_get_state(IMPORTED_SENSOR)
        exp = ha_get_state(EXPORTED_SENSOR)

        delta_import = max(0, imp - state["last_import"])
        delta_export = max(0, exp - state["last_export"])

        price = prices.get(current_int, 0)

        state["import_cost"] += delta_import * (price + TOTAL_TARIFF)
        state["export_value"] += delta_export * price

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
