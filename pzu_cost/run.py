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

    url = f"{HA_URL}/api/states/{entity_id}"
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()

    state = r.json().get("state")
    try:
        return float(state)
    except (TypeError, ValueError):
        return 0.0


def ha_set_state(entity_id, value, attributes=None):
    url = f"{HA_URL}/api/states/{entity_id}"

    payload = {
        "state": value,
        "attributes": attributes or {}
    }

    r = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    r.raise_for_status()

# =========================
# PZU
# =========================

def fetch_pzu_avg_price(calc_day):
    url = (
        f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/"
        f"{calc_day.day:02d}/{calc_day.month:02d}/{calc_day.year}/ro"
    )
    print("PZU URL:", url)

    r = requests.get(url, timeout=30)
    r.raise_for_status()

    root = ET.fromstring(r.text)

    prices = []
    for detail in root.findall(".//Detail"):
        price = float(detail.findtext("Price"))
        prices.append(price)

    if not prices:
        return 0.0

    # RON/MWh -> RON/kWh
    return (sum(prices) / len(prices)) / 1000.0

# =========================
# MAIN
# =========================

def main():
    print("IMPORTED_SENSOR =", IMPORTED_SENSOR)
    print("EXPORTED_SENSOR =", EXPORTED_SENSOR)

    # ziua precedenta (UTC)
    calc_day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    print("Calculation day:", calc_day)

    imported_kwh = ha_get_state(IMPORTED_SENSOR)
    exported_kwh = ha_get_state(EXPORTED_SENSOR)

    print("Imported kWh:", imported_kwh)
    print("Exported kWh:", exported_kwh)

    avg_pzu_price = fetch_pzu_avg_price(calc_day)
    print("Avg PZU price (RON/kWh):", avg_pzu_price)

    total_tariff = (
        TARIFF_DISTRIBUTION +
        TARIFF_TRANSPORT +
        TARIFF_OTHER
    )

    import_cost = imported_kwh * (avg_pzu_price + total_tariff)
    export_value = exported_kwh * avg_pzu_price

    print("Import cost RON:", import_cost)
    print("Export value RON:", export_value)

    ha_set_state(
        "sensor.pzu_import_cost",
        round(import_cost, 2),
        {
            "unit_of_measurement": "RON",
            "device_class": "monetary",
            "state_class": "total",
            "calculation_day": str(calc_day),
        }
    )

    ha_set_state(
        "sensor.pzu_export_value",
        round(export_value, 2),
        {
            "unit_of_measurement": "RON",
            "device_class": "monetary",
            "state_class": "total",
            "calculation_day": str(calc_day),
        }
    )

    print("Updated sensor.pzu_import_cost =", round(import_cost, 2))
    print("Updated sensor.pzu_export_value =", round(export_value, 2))


if __name__ == "__main__":
    main()
