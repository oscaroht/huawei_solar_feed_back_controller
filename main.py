"""Turn Huawei Solar inverter ON or OFF depending on the market price of the NordPool exchange.

it is meant to run in a crontab and restart every hour:
- get the price for the next hour
- decide strategy:
    - feed back as much as possible (price>0)
    - match supply and demand (price=0)
    - no generation (price<0)
"""

from pymodbus.client import ModbusTcpClient
import requests
from datetime import datetime, UTC, time, timedelta
import os
from zoneinfo import ZoneInfo
from enum import Enum
from homewizard_p1 import get_p1_data
import time

# price settings
FEED_IN_COST_PER_KWH = 0.0182  # charge on top of the NordPool electricity price for feeding in electricity
SUPPLY_COST_PER_KWH = 0.0182  # charge on top of the NordPool electricity price for supplying the grid

# inverter registry codes
PERCENTAGE_ACTIVE_POWER_DERATING_REGISTER = 40125  # % between 0 and 1000 for 0.0% to 100.0%. *No longer in use*
FIXED_KW_ACTIVE_POWER_DERATING_REGISTER = 40126  # 0 for 0W, 1000 for 1000W
ACTIVE_POWER_REGISTER = 32080  # Amount of power the panels deliver

class Status(Enum):
    """Determines what on and off mean. This is either max power or min power."""
    ON  = 3000
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
    """Logging to stdout and discord"""
    print(text)
    data = { "content": text }
    requests.request("POST", DISCORD_WEBHOOK_URL_OPPORTUNITIES,  data=data)

log("test if gets pulled")

def get_current_price():
    """Find the price of the current hour"""
    now = datetime.now(ZoneInfo("Europe/Amsterdam"))
    date = now.strftime("%d-%m-%Y")  # this really needs timezone AMS. This is no mistake
    
    url = f"https://public.api.energyzero.nl/public/v1/prices?date={date}&interval=INTERVAL_HOUR&energyType=ENERGY_TYPE_ELECTRICITY"
    headers = {
      'Accept': 'application/json'
    }

    res = requests.request("GET", url, headers=headers)

    res.raise_for_status()
    data = res.json()
   
    # in the response UTC is used instead of local time. Yes, really...
    utc_now = datetime.now(UTC)
    utc_start_time = utc_now.replace(minute=0, second=0, microsecond=0)
    start = utc_start_time.strftime("%Y-%m-%dT%H:%M:%SZ")  # format utc_now in the same way as in the api response
    end = utc_start_time + timedelta(hours=1)
    # iterate the response json to find the price for the current hour
    # when the current hour is found return the price value and the end of the price period
    for base in data.get('base', []):
        if base['start'] == start:
            return float(base['price']['value']), end

    raise Exception(f"Current price cannot be found in local {date} with utc range {data['range']}")

def write_register(register: int, value: list[int]):
    """Connect to any register and set a value."""
    with ModbusTcpClient(INVERTER_IP, port=PORT) as client:
        response = client.write_registers(register, value)
        if response.isError():
            raise Exception(f"Inverter Error Response: {response}")

try:
    price, end = get_current_price()

    if price > FEED_IN_COST_PER_KWH:
        print("Positive price next hour so turn inverter ON")
        status = Status.ON
        write_register(40126, [0, Status.ON.value])
    elif price < -1 * SUPPLY_COST_PER_KWH:
        log("Negative price next hour so turn inverter OFF")
        status = Status.OFF
        write_register(40126, [0, Status.OFF.value])
    else:
        print("Match internal use")
        while datetime.now(UTC) < end: # time.time() - start_time < (60 * 60 - 10):  # for the next hour
            print("Sleep 10s")
            time.sleep(10)  # p1 delivers a new telegram every 10sec
            p1 = get_p1_data()
            power_balance = p1['active_power_w']  # negative when feeding back
            print(f"active_power balance from p1 {power_balance} (negative when feeding back)")

            # we need to read what the max power is now and then subtract or add the active active_power from the p1 meter
            with ModbusTcpClient(INVERTER_IP, port=PORT) as client:
                response = client.read_holding_registers(FIXED_KW_ACTIVE_POWER_DERATING_REGISTER, count=2)
                if response.isError():
                    raise Exception(f"Inverter Error Response while reading FIXED_KW_ACTIVE_POWER_DERATING_REGISTER: {response}")
                _, max_power = response.registers
                print(f"Current max power {max_power}W")

                response = client.read_holding_registers(ACTIVE_POWER_REGISTER, count=2)
                if response.isError():
                    raise Exception(f"Inverter Error Response while reading ACTIVE_POWER_REGISTER: {response}")
                _, power_generated = response.registers
                print(f"Panels generate {power_generated}W")

                if max_power > power_generated:
                    print("The power limit is higher than the generated power.")
                    continue
                new_max_power = int(max_power + power_balance)
                response = client.write_registers(FIXED_KW_ACTIVE_POWER_DERATING_REGISTER, [0, new_max_power])
                if response.isError():
                    raise Exception(f"Inverter Error Response while writing FIXED_KW_ACTIVE_POWER_DERATING_REGISTER: {response}")
                print("Inverter set successfully.")

except Exception as e:
    log(str(e) + '::' + str(e.__traceback__))
    # in case of emergency try to set everything back the way it was
    write_register(FIXED_KW_ACTIVE_POWER_DERATING_REGISTER, [0, Status.ON.value])

