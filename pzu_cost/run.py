#!/usr/bin/env python3
import os
import json
import requests
import datetime
import xml.etree.ElementTree as ET

# =========================
# HOME ASSISTANT SETTINGS
# =========================

HA_URL = "http://supervisor/core/api"

SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN")
if not SUPERVISOR_TOKEN:
    raise RuntimeError("SUPERVISOR_TOKEN not available")

HEADERS = {
    "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
    "Content-Type": "application/json",
}

OPTIONS_FILE = "/data/options.json"

# =========================
# LOAD OPTIONS
# =========================

if not os.path.exists(OPTIONS_FILE):
    raise RuntimeError("options.json not found - addon not configured")

with open(OPTIONS_FILE, "r") as f:
    options = json.load(f)

IMPORTED_SENSOR = options.get("imported_sensor")
EXPORTED_SENSOR = options.get("exported_sensor")

TARIFF_DISTRIBUTION = float(options.get("tariff_distribution", 0))
TARIFF_TRANSPORT = float(options.get("tariff_transport", 0))
TARIFF_OTHER = float(options.get("tariff_other", 0))

print("IMPORTED_SENSOR =", IMPORTED_SENSOR)
print("EXPORTED_SENSOR =", EXPORTED_SENSOR)

if not IMPORTED_SENSOR or not EXPORTED_SENSOR:
    raise RuntimeError("Sensor entities not configured")

# =========================
# HELPERS
# =========================

def ha_get_state(entity_id: str) -> float:
    url = f"{HA_URL}/states/{entity_id}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()

    state = r.json().get("state")
    try:
        return float(state)
    except (TypeError, ValueError):
        return 0.0


def ha_set_state(entity_id: str, state, attributes=None):
    payload = {
        "state": state,
        "attributes": attributes or {}
    }

    url = f"{HA_URL}/states/{entity_id}"
    r = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    r.raise_for_status()


def get_pzu_average_price(date: datetime.date) -> float:
    url = f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/{date.day}/{date.month}/{date.year}/ro"
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
        raise RuntimeError("No PZU prices found")

    # PZU price is RON/MWh -> convert to RON/kWh
    avg_price_ron_kwh = (sum(prices) / len(prices)) / 1000.0
    return avg_price_ron_kwh


# =========================
# MAIN
# =========================

def main():
    # ziua precedenta
    calc_day = datetime.date.today() - datetime.timedelta(days=1)
    print("Calculation day:", calc_day)

    imported_kwh = ha_get_state(IMPORTED_SENSOR)
    exported_kwh = ha_get_state(EXPORTED_SENSOR)

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


if __name__ == "__main__":
    main()
