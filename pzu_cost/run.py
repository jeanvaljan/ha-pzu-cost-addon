import os
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, date, timezone
from collections import defaultdict
import json


print("OPTIONS EXISTS:", os.path.exists("/data/options.json"))

with open("/data/options.json") as f:
    print("OPTIONS RAW:", f.read())


SUPERVISOR = "http://supervisor/core/api"
TOKEN = os.getenv("SUPERVISOR_TOKEN")

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

with open("/data/options.json", "r") as f:
    options = json.load(f)

IMPORTED_SENSOR = options.get("imported_sensor")
EXPORTED_SENSOR = options.get("exported_sensor")

TARIFF_DIST = float(options.get("tariff_distribution", 0.0))
TARIFF_TRANS = float(options.get("tariff_transport", 0.0))
TARIFF_SYS = float(options.get("tariff_system", 0.0))
TARIFF_COG = float(options.get("tariff_cogeneration", 0.0))
VAT = float(options.get("vat", 0.0))

if not IMPORTED_SENSOR or not EXPORTED_SENSOR:
    raise RuntimeError(
        "Add-on not configured: set imported_sensor and exported_sensor in Configuration"
    )

print("IMPORTED_SENSOR =", IMPORTED_SENSOR)
print("EXPORTED_SENSOR =", EXPORTED_SENSOR)

from datetime import datetime, timedelta, timezone

def day_bounds_utc(day):
    start = datetime(
        year=day.year,
        month=day.month,
        day=day.day,
        tzinfo=timezone.utc
    )
    end = start + timedelta(days=1)
    return start, end

FIXED_TARIFF = TARIFF_DIST + TARIFF_TRANS + TARIFF_SYS + TARIFF_COG
def ha_history(start, end, entity_id):
    url = (
        f"{HA_URL}/api/history/period/{start}"
        f"?end_time={end}"
    )

    headers = {
        "Authorization": f"Bearer {SUPERVISOR_TOKEN}",
        "Content-Type": "application/json",
    }

    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()

    data = r.json()

    # data = list of lists, filtram manual entity_id
    for entity_history in data:
        if entity_history and entity_history[0]["entity_id"] == entity_id:
            return entity_history

    return []

def interval_15(ts):
    return ts.hour * 4 + ts.minute // 15 + 1

def aggregate(history):
    data = defaultdict(float)
    for r in history:
        ts = datetime.fromisoformat(r["last_changed"])
        data[interval_15(ts)] += float(r["state"])
    return data

def pzu_prices(day):
    url = f"https://opcom.ro/rapoarte-pzu-raportPIP-export-xml/{day.day}/{day.month}/{day.year}/ro"
    r = requests.get(url, timeout=10)
    r.raise_for_status()

    root = ET.fromstring(r.text)
    return {
        int(d.find("Interval").text): float(d.find("Price").text) / 1000
        for d in root.iter("Detail")
    }

def set_sensor(name, value, calc_date=None):
    attrs = {
        "unit_of_measurement": "RON",
        "device_class": "monetary",
        "state_class": "total_increasing"
    }
    if calc_date:
        attrs["calculation_date"] = calc_date.isoformat()

    requests.post(
        f"{SUPERVISOR}/states/{name}",
        headers=HEADERS,
        json={
            "state": round(value, 3),
            "attributes": attrs
        }
    )



def calculate_day(day):
    from datetime import datetime, timedelta, timezone

    start = datetime.combine(day, datetime.min.time()).replace(tzinfo=timezone.utc)
    end = start + timedelta(days=1)


    imp = aggregate(ha_history(IMPORTED_SENSOR, day))
    exp = aggregate(ha_history(EXPORTED_SENSOR, day))
    prices = pzu_prices(day)

    energy_cost = sum(imp[i] * prices.get(i, 0) for i in imp)
    inject_value = sum(exp[i] * prices.get(i, 0) for i in exp)

    fixed_cost = sum(imp.values()) * FIXED_TARIFF
    subtotal = energy_cost + fixed_cost
    total = subtotal * (1 + VAT)

    return total, inject_value, total - inject_value

def main():
    calc_day = date.today() - timedelta(days=1)
    cost, inject, sold = calculate_day(calc_day)

    set_sensor("sensor.pzu_daily_cost", cost, calc_day)
    set_sensor("sensor.pzu_daily_injected_value", inject, calc_day)
    set_sensor("sensor.pzu_daily_sold", sold, calc_day)

    print("OPTIONS EXISTS:", os.path.exists("/data/options.json"))

    with open("/data/options.json") as f:
        print("OPTIONS RAW:", f.read())



while True:
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
    time.sleep(3600)

