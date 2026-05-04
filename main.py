"""Turn Huawei Solar inverter ON or OFF depending on the market price of the NordPool exchange."""

from pymodbus.client import ModbusTcpClient
import requests
from datetime import datetime, UTC, time, timedelta
import os
from zoneinfo import ZoneInfo
from enum import Enum
from homewizard_p1 import get_p1_data
import time

FEED_IN_COST_PER_KWH = 0.0182
SUPPLY_COST_PER_KWH = 0.0182

PERCENTAGE_ACTIVE_POWER_DERATING_REGISTER = 40125  # % between 0 and 1000 for 0.0% to 100.0%
FIXED_KW_ACTIVE_POWER_DERATING_REGISTER = 40126  # 0 for 0W, 1000 for 1000W
ACTIVE_POWER_REGISTER = 32080  # Amount of power the panels deliver

class Status(Enum):
    """Determines what on and off mean."""
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
    end = utc_start_time + timedelta(hours=1)
    for base in data.get('base', []):
        if base['start'] == start:
            return float(base['price']['value']), end

    raise Exception(f"Current price cannot be found in local {date} with utc range {data['range']}")

def write_register(register: int, value: list[int]):
    """Connect to any register and set a value"""
    with ModbusTcpClient(INVERTER_IP, port=PORT) as client:
        response = client.write_registers(register, value)
        if response.isError():
            raise Exception(f"Inverter Error Response: {response}")

def read_register(register: int, count=1):
    with ModbusTcpClient(INVERTER_IP, port=PORT) as client:
        response = client.read_holding_registers(register, count=count)
        if response.isError():
            raise Exception(f"Inverter Error Response: {response}")
    return response.registers

def switch_inverter(value: int):
    """Connect to inverter and set address 40125 to ON (1000) or OFF (1000).

    See modbus_docs.pdf for additional info
    """
    with ModbusTcpClient(INVERTER_IP, port=PORT) as client:
        response = client.write_registers(40120, [value])
        if response.isError():
            raise Exception(f"Inverter Error Response: {response}")
    print("Inverter set successfully")


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
        start_time = time.time()  # seconds since 1970
        print("Match internal use")
        while datetime.now(UTC) < end: # time.time() - start_time < (60 * 60 - 10):  # for the next hour
            print("Sleep 10s")
            time.sleep(10)
            p1 = get_p1_data()
            power_balance = p1['active_power_w']  # negative when feeding back
            print(f"active_power balance from p1 {power_balance} (negative when feeding back)")

            # we need to read what the max power is now and then subtract or add the active active_power from the p1 meter
            with ModbusTcpClient(INVERTER_IP, port=PORT) as client:
                response = client.read_holding_registers(40126, count=2)
                if response.isError():
                    raise Exception(f"Inverter Error Response: {response}")
                _, max_power = response.registers
                print(f"Current max power {max_power}W")

                response = client.read_holding_registers(ACTIVE_POWER_REGISTER, count=2)
                if response.isError():
                    raise Exception(f"Inverter Error Response: {response}")
                _, power_generated = response.registers
                print(f"Panels generate {power_generated}W")

                if max_power > power_generated:
                    print("The power limit is higher than the generated power.")
                    continue
                new_max_power = int(max_power + power_balance)
                response = client.write_registers(40126, [0, new_max_power])
                print("Inverter set successfully.")

except Exception as e:
    log(str(e) + '::' + str(e.__traceback__))
    write_register(40126, [0, 3000])

