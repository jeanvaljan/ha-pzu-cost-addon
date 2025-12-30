import os
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, date
from collections import defaultdict

SUPERVISOR = "http://supervisor/core/api"
TOKEN = os.environ["SUPERVISOR_TOKEN"]
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

IMPORTED_SENSOR = os.getenv("IMPORTED_SENSOR")
EXPORTED_SENSOR = os.getenv("EXPORTED_SENSOR")

def env_float(name, default):
    val = os.getenv(name)
    return float(val) if val not in (None, "") else default

TARIFF_DIST = env_float("TARIFF_DISTRIBUTION", 0.0)
TARIFF_TRANS = env_float("TARIFF_TRANSPORT", 0.0)
TARIFF_SYS = env_float("TARIFF_SYSTEM", 0.0)
TARIFF_COG = env_float("TARIFF_COGENERATION", 0.0)
VAT = env_float("VAT", 0.0)


FIXED_TARIFF = TARIFF_DIST + TARIFF_TRANS + TARIFF_SYS + TARIFF_COG

def ha_history(sensor, start, end):
    url = f"{SUPERVISOR}/history/period/{start.isoformat()}"
    r = requests.get(url, headers=HEADERS, params={
        "filter_entity_id": sensor,
        "end_time": end.isoformat()
    })
    r.raise_for_status()
    return r.json()[0]

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

def set_sensor(name, value):
    requests.post(
        f"{SUPERVISOR}/states/{name}",
        headers=HEADERS,
        json={
            "state": round(value, 3),
            "attributes": {
                "unit_of_measurement": "RON",
                "device_class": "monetary",
                "state_class": "total_increasing",
                "friendly_name": name.replace("_", " ").title()
            }
        }
    )


def calculate_day(day):
    start = datetime.combine(day, datetime.min.time())
    end = start + timedelta(days=1)

    imp = aggregate(ha_history(IMPORTED_SENSOR, start, end))
    exp = aggregate(ha_history(EXPORTED_SENSOR, start, end))
    prices = pzu_prices(day)

    energy_cost = sum(imp[i] * prices.get(i, 0) for i in imp)
    inject_value = sum(exp[i] * prices.get(i, 0) for i in exp)

    fixed_cost = sum(imp.values()) * FIXED_TARIFF
    subtotal = energy_cost + fixed_cost
    total = subtotal * (1 + VAT)

    return total, inject_value, total - inject_value

def main():
    day = date.today() - timedelta(days=1)
    cost, inject, sold = calculate_day(day)

    set_sensor("sensor.pzu_daily_cost", cost)
    set_sensor("sensor.pzu_daily_injected_value", inject)
    set_sensor("sensor.pzu_daily_sold", sold)


while True:
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
    time.sleep(3600)

