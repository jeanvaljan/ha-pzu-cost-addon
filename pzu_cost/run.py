import os
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# =========================
# CONFIG DIN ENV
# =========================

HA_URL = "http://supervisor/core"
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN")

IMPORTED_SENSOR = os.getenv("IMPORTED_SENSOR")
EXPORTED_SENSOR = os.getenv("EXPORTED_SENSOR")

TARIFF_DISTRIBUTION = float(os.getenv("TARIFF_DISTRIBUTION", "0.0"))
TARIFF_TRANSPORT = float(os.getenv("TARIFF_TRANSPORT", "0.0"))
TARIFF_OTHER = float(os.getenv("TARIFF_OTHER", "0.0"))

HEADERS = {
    "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
    "Content-Type": "application/json",
}

# =========================
# HA HELPERS
# =========================

def ha_get_state(entity_id):
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
    url = f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/{calc_day.day:02d}/{calc_day.month:02d}/{calc_day.year}/ro"
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

    # Preturile sunt in RON/MWh -> convertim in RON/kWh
    avg_price_ron_kwh = (sum(prices) / len(prices)) / 1000.0
    return avg_price_ron_kwh


# =========================
# MAIN
# =========================

def main():
    print("IMPORTED_SENSOR =", IMPORTED_SENSOR)
    print("EXPORTED_SENSOR =", EXPORTED_SENSOR)

    # calcul pentru ziua precedenta
    calc_day = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    print("Calculation day:", calc_day)

    # energie zilnica (kWh)
    imported_kwh = ha_get_state(IMPORTED_SENSOR)
    exported_kwh = ha_get_state(EXPORTED_SENSOR)

    print("Imported kWh:", imported_kwh)
    print("Exported kWh:", exported_kwh)

    # pret PZU mediu
    avg_pzu_price = fetch_pzu_avg_price(calc_day)
    print("Avg PZU price (RON/kWh):", avg_pzu_price)

    # tarife totale import
    total_tariff = TARIFF_DISTRIBUTION + TARIFF_TRANSPORT + TARIFF_OTHER

    # calcule
    import_cost = imported_kwh * (avg_pzu_price + total_tariff)
    export_value = exported_kwh * avg_pzu_price

    print("Import cost RON:", import_cost)
    print("Export value RON:", export_value)

    # publicare senzori
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
