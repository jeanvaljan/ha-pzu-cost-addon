#!/usr/bin/env python3

import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

# =========================================================
# Home Assistant Supervisor API
# =========================================================

SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN")

if not SUPERVISOR_TOKEN:
    try:
        with open("/var/run/secrets/supervisor_token") as f:
            SUPERVISOR_TOKEN = f.read().strip()
    except FileNotFoundError:
        raise RuntimeError("Supervisor token not available")

SUPERVISOR_URL = "http://supervisor/core/api"

HEADERS = {
    "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
    "Content-Type": "application/json",
}


# =========================================================
# Load addon options
# =========================================================

OPTIONS_PATH = "/data/options.json"
OPTIONS = {}

if os.path.exists(OPTIONS_PATH):
    with open(OPTIONS_PATH) as f:
        OPTIONS = json.load(f)

IMPORTED_SENSOR = OPTIONS.get("imported_sensor")
EXPORTED_SENSOR = OPTIONS.get("exported_sensor")

TARIFF_DISTRIBUTION = float(OPTIONS.get("tariff_distribution", 0))
TARIFF_TRANSPORT = float(OPTIONS.get("tariff_transport", 0))
TARIFF_OTHER = float(OPTIONS.get("tariff_other", 0))

print("IMPORTED_SENSOR =", IMPORTED_SENSOR)
print("EXPORTED_SENSOR =", EXPORTED_SENSOR)

# =========================================================
# Helper functions
# =========================================================

def ha_get_state(entity_id):
    """Read HA sensor state as float"""
    url = f"{SUPERVISOR_URL}/states/{entity_id}"
    r = requests.get(url, headers=HEADERS, timeout=10)
    r.raise_for_status()
    state = r.json().get("state")
    try:
        return float(state)
    except (TypeError, ValueError):
        return None


def ha_get_state_safe(entity_id):
    """Return state or 0 if sensor does not exist"""
    try:
        value = ha_get_state(entity_id)
        return value if value is not None else 0.0
    except Exception:
        return 0.0


def ha_set_state(entity_id, state, attributes=None):
    url = f"{SUPERVISOR_URL}/states/{entity_id}"
    payload = {
        "state": state,
        "attributes": attributes or {}
    }
    r = requests.post(url, headers=HEADERS, json=payload, timeout=10)
    r.raise_for_status()


def get_pzu_average_price(calc_day):
    """Fetch PZU prices and return average RON/kWh"""
    url = f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/{calc_day.day:02d}/{calc_day.month:02d}/{calc_day.year}/ro"
    print("PZU URL:", url)

    r = requests.get(url, timeout=20)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    prices = []

    for detail in root.iter("Detail"):
        price_mwh = float(detail.find("Price").text)
        prices.append(price_mwh / 1000.0)  # RON/kWh

    if not prices:
        return None

    return sum(prices) / len(prices)

# =========================================================
# MAIN
# =========================================================

def main():
    # -----------------------------------------------------
    # 1. Calculation day = yesterday (UTC-safe)
    # -----------------------------------------------------
    calc_day = (datetime.utcnow() - timedelta(days=1)).date()
    print(f"Calculation day: {calc_day}")

    # -----------------------------------------------------
    # 2. Validate configuration
    # -----------------------------------------------------
    if not IMPORTED_SENSOR or not EXPORTED_SENSOR:
        print("ERROR: Sensors not configured")
        return

    # -----------------------------------------------------
    # 3. Read daily energy sensors
    # -----------------------------------------------------
    imported_kwh = ha_get_state(IMPORTED_SENSOR)
    exported_kwh = ha_get_state(EXPORTED_SENSOR)

    if imported_kwh is None or exported_kwh is None:
        print("Daily sensors not ready yet")
        return

    print(f"Imported kWh: {imported_kwh}")
    print(f"Exported kWh: {exported_kwh}")

    # -----------------------------------------------------
    # 4. PZU average price
    # -----------------------------------------------------
    avg_pzu_price = get_pzu_average_price(calc_day)
    if avg_pzu_price is None:
        print("No PZU data")
        return

    print(f"Avg PZU price (RON/kWh): {avg_pzu_price}")

    # -----------------------------------------------------
    # 5. Tariffs
    # -----------------------------------------------------
    total_tariff = (
        TARIFF_DISTRIBUTION +
        TARIFF_TRANSPORT +
        TARIFF_OTHER
    )

    final_import_price = avg_pzu_price + total_tariff

    # -----------------------------------------------------
    # 6. Cost calculation
    # -----------------------------------------------------
    import_cost = imported_kwh * final_import_price
    export_value = exported_kwh * avg_pzu_price

    print(f"Import cost RON: {import_cost}")
    print(f"Export value RON: {export_value}")

    # -----------------------------------------------------
    # 7. Daily sensors
    # -----------------------------------------------------
    ha_set_state(
        "sensor.pzu_import_cost",
        round(import_cost, 2),
        {
            "unit_of_measurement": "RON",
            "device_class": "monetary",
            "state_class": "measurement",
            "friendly_name": "PZU Import Cost (Daily)",
            "calculation_day": str(calc_day),
        }
    )

    ha_set_state(
        "sensor.pzu_export_value",
        round(export_value, 2),
        {
            "unit_of_measurement": "RON",
            "device_class": "monetary",
            "state_class": "measurement",
            "friendly_name": "PZU Export Value (Daily)",
            "calculation_day": str(calc_day),
        }
    )

    # -----------------------------------------------------
    # 8. Total sensors
    # -----------------------------------------------------
    import_total = ha_get_state_safe("sensor.pzu_import_cost_total")
    export_total = ha_get_state_safe("sensor.pzu_export_value_total")

    import_total += import_cost
    export_total += export_value

    ha_set_state(
        "sensor.pzu_import_cost_total",
        round(import_total, 2),
        {
            "unit_of_measurement": "RON",
            "device_class": "monetary",
            "state_class": "total_increasing",
            "friendly_name": "PZU Import Cost (Total)",
        }
    )

    ha_set_state(
        "sensor.pzu_export_value_total",
        round(export_total, 2),
        {
            "unit_of_measurement": "RON",
            "device_class": "monetary",
            "state_class": "total_increasing",
            "friendly_name": "PZU Export Value (Total)",
        }
    )

    print("PZU daily calculation finished successfully")

# =========================================================
# RUN ONCE AND EXIT (NO RESTART LOOP)
# =========================================================

if __name__ == "__main__":
    main()
    time.sleep(5)
