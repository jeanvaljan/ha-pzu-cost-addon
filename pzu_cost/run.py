#!/usr/bin/env python3

import requests
import json
import os
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

# ------------------------------------------------------------
# CONFIG HA
# ------------------------------------------------------------
HA_URL = "http://supervisor/core"
SUPERVISOR_TOKEN = os.getenv("SUPERVISOR_TOKEN")

HEADERS = {
    "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
    "Content-Type": "application/json",
}

# ------------------------------------------------------------
# READ ADD-ON OPTIONS (CORRECT WAY)
# ------------------------------------------------------------
OPTIONS_PATH = "/data/options.json"

if not os.path.exists(OPTIONS_PATH):
    raise RuntimeError("options.json not found – add-on not configured")

with open(OPTIONS_PATH, "r") as f:
    OPTIONS = json.load(f)

IMPORTED_SENSOR = OPTIONS.get("imported_sensor")
EXPORTED_SENSOR = OPTIONS.get("exported_sensor")

if not IMPORTED_SENSOR or not EXPORTED_SENSOR:
    raise RuntimeError(
        "Add-on not configured. Set imported_sensor and exported_sensor in Configuration"
    )

print("IMPORTED_SENSOR =", IMPORTED_SENSOR)
print("EXPORTED_SENSOR =", EXPORTED_SENSOR)

# ------------------------------------------------------------
# TIME HELPERS
# ------------------------------------------------------------
def ha_ts(dt: datetime) -> str:
    """Return HA-compatible UTC timestamp (no +00:00)"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------
# HOME ASSISTANT HISTORY API
# ------------------------------------------------------------

def ha_energy_statistics(start_dt, entity_id):
    start = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (start_dt + timedelta(days=1, seconds=-1)).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = (
        f"{HA_URL}/api/history/statistics/period/{start}"
        f"?end_time={end}"
        f"&statistic_ids={entity_id}"
        f"&types=sum"
    )

    print("Statistics URL:", url)

    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()

    data = r.json()

    if not data or entity_id not in data:
        return 0.0

    stats = data[entity_id]
    if not stats:
        return 0.0

    return float(stats[-1]["sum"])



# ------------------------------------------------------------
# ENERGY CALCULATION (DELTA kWh)
# ------------------------------------------------------------
def energy_delta(history):
    """
    history: list of state dicts
    returns kWh consumed/exported during period
    """
    if not history or len(history) < 2:
        return 0.0

    try:
        start = float(history[0]["state"])
        end = float(history[-1]["state"])
        return max(0.0, end - start)
    except Exception:
        return 0.0


# ------------------------------------------------------------
# OPCOM PZU PRICE (XML)
# ------------------------------------------------------------
def get_pzu_prices(calc_date):
    """
    Returns dict: interval (1-96) -> price (RON/MWh)
    """
    url = f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/{calc_date.day:02d}/{calc_date.month:02d}/{calc_date.year}/ro"
    print("PZU URL:", url)

    r = requests.get(url, timeout=30)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    prices = {}

    for detail in root.findall(".//Detail"):
        interval = int(detail.findtext("Interval"))
        price = float(detail.findtext("Price"))
        prices[interval] = price

    return prices


# ------------------------------------------------------------
# MAIN LOGIC
# ------------------------------------------------------------
def main():
    # ---- calculate for PREVIOUS DAY ----
    calc_day = datetime.now(timezone.utc).date() - timedelta(days=1)
    print("Calculation day:", calc_day)

    start_dt = datetime.combine(calc_day, datetime.min.time(), tzinfo=timezone.utc)
    #end_dt = start_dt + timedelta(days=1)

    # ---- get HA history ----
    imported_kwh = ha_energy_statistics(start_dt, IMPORTED_SENSOR)
    exported_kwh = ha_energy_statistics(start_dt, EXPORTED_SENSOR)

    print("Imported kWh:", imported_kwh)
    print("Exported kWh:", exported_kwh)

    # ---- get PZU prices ----
    pzu_prices = get_pzu_prices(calc_day)

    # ---- average daily PZU price ----
    if not pzu_prices:
        raise RuntimeError("No PZU prices received")

    avg_pzu_price_mwh = sum(pzu_prices.values()) / len(pzu_prices)
    avg_pzu_price_kwh = avg_pzu_price_mwh / 1000.0

    print("Avg PZU price (RON/kWh):", avg_pzu_price_kwh)

    # ---- cost calculation ----
    cost_import = imported_kwh * avg_pzu_price_kwh
    value_export = exported_kwh * avg_pzu_price_kwh

    print("Import cost RON:", cost_import)
    print("Export value RON:", value_export)

    # ---- publish sensors ----
    publish_sensor(
        "sensor.pzu_import_cost",
        round(cost_import, 2),
        "RON",
        "monetary",
    )

    publish_sensor(
        "sensor.pzu_export_value",
        round(value_export, 2),
        "RON",
        "monetary",
    )


# ------------------------------------------------------------
# PUBLISH SENSOR STATE
# ------------------------------------------------------------
def publish_sensor(entity_id, value, unit, device_class):
    url = f"{HA_URL}/api/states/{entity_id}"

    payload = {
        "state": value,
        "attributes": {
            "unit_of_measurement": unit,
            "device_class": device_class,
            "friendly_name": entity_id.replace("_", " ").title(),
        },
    }

    r = requests.post(url, headers=HEADERS, json=payload, timeout=30)
    r.raise_for_status()

    print("Updated", entity_id, "=", value)


# ------------------------------------------------------------
# ENTRYPOINT
# ------------------------------------------------------------
if __name__ == "__main__":
    main()
