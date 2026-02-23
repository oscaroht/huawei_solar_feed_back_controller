"""Turn Huawei Solar inverter ON or OFF depending on the market price of the NordPool exchange."""

from pymodbus.client import ModbusTcpClient
import requests
from datetime import datetime, UTC
import os
from zoneinfo import ZoneInfo
from enum import Enum

class Status(Enum):
    ON  = 1000
    OFF = 0

# collect and check the env variable
DISCORD_WEBHOOK_URL_OPPORTUNITIES = os.environ.get("DISCORD_WEBHOOK_URL_OPPORTUNITIES", "")
if not DISCORD_WEBHOOK_URL_OPPORTUNITIES:
    raise Exception("DISCORD_WEBHOOK_URL_OPPORTUNITIES env var not set")
INVERTER_IP = os.environ.get("INVERTER_IP", "")
if not INVERTER_IP:
    raise Exception("INVERTER_IP env var not set")
PORT = int(os.environ.get("PORT", "0"))
if not PORT:
    raise Exception("PORT env var not set")

def log(text: str):
    """Extensive logging to stdout and discord"""
    print(text)
    data = { "content": text }
    requests.request("POST", DISCORD_WEBHOOK_URL_OPPORTUNITIES,  data=data)


def get_current_price():
    """Find the price of the current hour"""
    now = datetime.now(ZoneInfo("Europe/Amsterdam"))
    date = now.strftime("%d-%m-%Y")
    
    url = f"https://public.api.energyzero.nl/public/v1/prices?date={date}&interval=INTERVAL_HOUR&energyType=ENERGY_TYPE_ELECTRICITY"
    headers = {
      'Accept': 'application/json'
    }

    res = requests.request("GET", url, headers=headers)

    res.raise_for_status()
    data = res.json()
   
    # in the response UTC is used instead of local time
    utc_now = datetime.now(UTC)
    utc_start_time = utc_now.replace(minute=0, second=0, microsecond=0, tzinfo=None)
    start = utc_start_time.strftime("%Y-%m-%dT%H:%M:%SZ")  # format utc_now in the same way as in the api response
    for base in data.get('base', []):
        if base['start'] == start:
            return float(base['price']['value'])

    raise Exception(f"Current price cannot be found in local {date} with utc range {data['range']}")


def switch_inverter(status: Status):
    """Connect to inverter and set address 40125 to ON (1000) or OFF (1000).

    See modbus_docs.pdf for additional info
    """
    with ModbusTcpClient(INVERTER_IP, port=PORT) as client:
        response = client.write_registers(40125, [status.value])
        if response.isError():
            raise Exception(f"Inverter Error Response: {response}")
    print("Inverter set successfully")


try:
    price = get_current_price()

    if price > 0.0:
        log("Positive price next hour so turn inverter ON")
        status = Status.ON
    else:
        log("Negative price next hour so turn inverter OFF")
        status = Status.OFF

    switch_inverter(status)

except Exception as e:
    log(str(e) + '::' + str(e.__traceback__))
    
