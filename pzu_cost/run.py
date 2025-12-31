#!/usr/bin/env python3
import os
import json
import time
import requests
import datetime
import xml.etree.ElementTree as ET

# ==================================================
# HOME ASSISTANT API
# ==================================================

HA_URL = "http://supervisor/core/api"

SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN")
if not SUPERVISOR_TOKEN:
    raise RuntimeError("SUPERVISOR_TOKEN not available")

HEADERS = {
    "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
    "Content-Type": "application/json",
}

OPTIONS_FILE = "/data/options.json"

# ==================================================
# LOAD ADD-ON OPTIONS
# ==================================================

if not os.path.exists(OPTIONS_FILE):
    raise RuntimeError("options.json not found. Configure the addon first.")

with open(OPTIONS_FILE, "r") as f:
    options = json.load(f)

IMPORTED_SENSOR = options.get("imported_sensor")
EXPORTED_SENSOR = options.get("exported_sensor")

TARIFF_DISTRIBUTION = float(options.get("tariff_distribution", 0.0))
TARIFF_TRANSPORT = float(options.get("tariff_transport", 0.0))
TARIFF_OTHER = float(options.get("tariff_other", 0.0))

print("IMPORTED_SENSOR =", IMPORTED_SENSOR)
print("EXPORTED_SENSOR =", EXPORTED_SENSOR)

if not IMPORTED_SENSOR or not EXPORTED_SENSOR:
    raise RuntimeError("Sensor entities not configured in addon options")
    
RUN_HOUR = int(options.get("run_hour", 0))
RUN_MINUTE = int(options.get("run_minute", 10))

print(f"Configured run time: {RUN_HOUR:02d}:{RUN_MINUTE:02d}")

# ==================================================
# HOME ASSISTANT HELPERS
# ==================================================

def ha_get_state(entity_id: str) -> float | None:
    url = f"{HA_URL}/states/{entity_id}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()

    state = r.json().get("state")

    if state in ("unknown", "unavailable", None):
        return None

    try:
        return float(state)
    except ValueError:
        return None

def ha_get_state_safe(entity_id: str) -> float:
    try:
        return ha_get_state(entity_id) or 0.0
    except Exception:
        return 0.0


def ha_set_state(entity_id: str, state, attributes=None):
    payload = {
        "state": state,
        "attributes": attributes or {}
    }
    url = f"{HA_URL}/states/{entity_id}"
    r = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    r.raise_for_status()

# ==================================================
# PZU PRICE
# ==================================================

def get_pzu_average_price(calc_date: datetime.date) -> float:
    url = (
        f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/"
        f"{calc_date.day}/{calc_date.month}/{calc_date.year}/ro"
    )
    print("PZU URL:", url)

    r = requests.get(url, timeout=60)
    r.raise_for_status()

    root = ET.fromstring(r.text)

    prices = []
    for detail in root.iter("Detail"):
        price = detail.findtext("Price")
        if price:
            prices.append(float(price))

    if not prices:
        raise RuntimeError("No PZU prices found in XML")

    # RON/MWh -> RON/kWh
    return (sum(prices) / len(prices)) / 1000.0

# ==================================================
# MAIN LOGIC
# ==================================================

def main():
    calc_day = datetime.date.today() - datetime.timedelta(days=1)
    print("Calculation day:", calc_day)

    # ---- cumulative sensors ----
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
            "state_class": "total_increasing"
        }
    )

    ha_set_state(
        "sensor.pzu_export_value_total",
        round(export_total, 2),
        {
            "unit_of_measurement": "RON",
            "device_class": "monetary",
            "state_class": "total_increasing"
        }
    )


    imported_kwh = ha_get_state(IMPORTED_SENSOR)
    exported_kwh = ha_get_state(EXPORTED_SENSOR)

    if imported_kwh is None or exported_kwh is None:
        print("Energy sensors not ready yet, skipping calculation")
        return


    print("Imported kWh:", imported_kwh)
    print("Exported kWh:", exported_kwh)

    avg_pzu_price = get_pzu_average_price(calc_day)
    print("Avg PZU price (RON/kWh):", avg_pzu_price)

    total_tariff = TARIFF_DISTRIBUTION + TARIFF_TRANSPORT + TARIFF_OTHER
    final_import_price = avg_pzu_price + total_tariff

    import_cost = imported_kwh * final_import_price
    export_value = exported_kwh * avg_pzu_price

    print("Import cost RON:", import_cost)
    print("Export value RON:", export_value)

    ha_set_state(
        "sensor.pzu_import_cost",
        round(import_cost, 2),
        {
            "unit_of_measurement": "RON",
            "device_class": "monetary",
            "state_class": "total"
        }
    )

    ha_set_state(
        "sensor.pzu_export_value",
        round(export_value, 2),
        {
            "unit_of_measurement": "RON",
            "device_class": "monetary",
            "state_class": "total"
        }
    )

    print("Sensors updated successfully")

# ==================================================
# SERVICE LOOP (PREVENTS RESTART LOOP)
# ==================================================

if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print("ERROR:", e)

        now = datetime.datetime.now()

        next_run = now.replace(
            hour=RUN_HOUR,
            minute=RUN_MINUTE,
            second=0,
            microsecond=0
        )

        if next_run <= now:
            next_run += datetime.timedelta(days=1)

        sleep_seconds = (next_run - now).total_seconds()
        if sleep_seconds < 60:
            sleep_seconds = 60

        print(f"Sleeping until {next_run} ({int(sleep_seconds)} seconds)")
        time.sleep(sleep_seconds)
