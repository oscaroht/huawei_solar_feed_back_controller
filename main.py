"""Turn Huawei Solar inverter ON or OFF depending on the market price of the NordPool exchange.

it runs as a long-lived process (e.g. in Docker) and, while the sun is above a set elevation (default -8°, i.e. some twilight),
starts a new cycle every hour:
- get the price for the next hour
- decide strategy:
    - feed back as much as possible (price>0)
    - match supply and demand (price=0)
    - no generation (price<0)

Outside the daylight window it sleeps. At the end of the window and on shutdown (SIGTERM/SIGINT/SIGHUP)
the inverter is set back to full power.
"""

from pymodbus.client import ModbusTcpClient
import requests
from datetime import datetime, UTC, timedelta
import os
from zoneinfo import ZoneInfo
from enum import Enum
from homewizard_p1 import get_p1_data
import time
import signal
from astral import Observer
from astral.sun import time_at_elevation, SunDirection

# price settings
FEED_IN_COST_PER_KWH = 0.0182  # charge on top of the NordPool electricity price for feeding in electricity
SUPPLY_COST_PER_KWH = 0.0182  # charge on top of the NordPool electricity price for supplying the grid

# inverter registry codes
PERCENTAGE_ACTIVE_POWER_DERATING_REGISTER = 40125  # % between 0 and 1000 for 0.0% to 100.0%. *No longer in use*
FIXED_KW_ACTIVE_POWER_DERATING_REGISTER = 40126  # 0 for 0W, 1000 for 1000W
ACTIVE_POWER_REGISTER = 32080  # Amount of power the panels deliver

P1_REFRESH_SECONDS = 20
RUN_OFFSET_SECONDS = 5  # start each cycle a few seconds after the hour
LOCAL_TZ = ZoneInfo("Europe/Amsterdam")

class Status(Enum):
    """Determines what on and off mean. This is either max power or min power."""
    ON  = 4000
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
# location of the panels, for sunrise/sunset. Defaults to the middle of the Netherlands.
LATITUDE = float(os.environ.get("LATITUDE", "52.09"))
LONGITUDE = float(os.environ.get("LONGITUDE", "5.12"))
# Angle of the SUN relative to the horizon (not a location!) above which the inverter may be producing.
# 0 = sun on the horizon, negative = below it (twilight). Official sunrise/sunset is -0.833.
# In Utrecht, -8 gives ~56 min before sunrise/after sunset in winter, ~47 at the equinox and ~72 in summer.
SUN_ELEVATION_DEGREES = float(os.environ.get("SUN_ELEVATION_DEGREES", "-8"))

def log(text: str):
    """Logging to stdout and discord"""
    print(text)
    data = { "content": text }
    requests.request("POST", DISCORD_WEBHOOK_URL_OPPORTUNITIES,  data=data, timeout=10)

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

def run_once(window_end: datetime):
    """One hourly control cycle (what the crontab used to start). Never runs past window_end."""
    try:
        price, end = get_current_price()
        end = min(end, window_end)  # stop matching when it gets dark

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
            while datetime.now(UTC) < end: # while the current price is valid
                print(f"Sleep {P1_REFRESH_SECONDS}s")
                time.sleep(P1_REFRESH_SECONDS)  # p1 delivers a new telegram every 10sec
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

                    power_consumed = power_balance + power_generated   # power_balance is negative when feeding back
                    print(f"Power consumed: {power_consumed}W")

                    if power_consumed < 0:
                        # the power_generated has a 0 sampling rate while the p1 has a ...sec sampling rate
                        # this leads to old telegrams being send over the api that distort the calculation
                        print("Data not synced. Continue")
                        time.sleep(1)
                        continue

                    new_max_power = min(int(power_consumed), Status.ON.value)
                    if new_max_power == max_power:
                        print("No change needed")
                        continue
                    print(f"New fixed max power: {new_max_power}W")
                    response = client.write_registers(FIXED_KW_ACTIVE_POWER_DERATING_REGISTER, [0, new_max_power])
                    if response.isError():
                        raise Exception(f"Inverter Error Response while writing FIXED_KW_ACTIVE_POWER_DERATING_REGISTER: {response}")
                    print("Inverter set successfully.")

    except Exception as e:
        log(str(e) + '::' + str(e.__traceback__))
        # in case of emergency try to set everything back the way it was
        write_register(FIXED_KW_ACTIVE_POWER_DERATING_REGISTER, [0, Status.ON.value])


def seconds_until_next_run(cycle_start: datetime) -> float:
    """Seconds until the top of the hour after the hour in which the cycle started.

    Based on the cycle start (not 'now') so that a 'match' cycle that overshoots the
    hour boundary by a few seconds runs the next cycle immediately instead of skipping an hour.
    """
    next_run = cycle_start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, seconds=RUN_OFFSET_SECONDS)
    return max(0.0, (next_run - datetime.now(UTC)).total_seconds())


def daylight_window(now: datetime) -> tuple[datetime, datetime]:
    """Return (start, end) of the current daylight window, or of the next one if today's has passed."""
    observer = Observer(latitude=LATITUDE, longitude=LONGITUDE)
    day = now.astimezone(LOCAL_TZ).date()
    for _ in range(2):  # today, else tomorrow
        start = time_at_elevation(observer, SUN_ELEVATION_DEGREES, day, SunDirection.RISING, LOCAL_TZ)
        end = time_at_elevation(observer, SUN_ELEVATION_DEGREES, day, SunDirection.SETTING, LOCAL_TZ)
        if now < end:
            return start, end
        day += timedelta(days=1)
    raise RuntimeError("unreachable")


def sleep_until(moment: datetime, reason: str):
    seconds = (moment - datetime.now(UTC)).total_seconds()
    if seconds > 0:
        print(f"Sleeping until {moment.astimezone(LOCAL_TZ):%Y-%m-%d %H:%M:%S} ({reason})")
        time.sleep(seconds)


def reset_to_normal(notify_on_failure: bool):
    """Best effort: put the inverter back to full power."""
    try:
        write_register(FIXED_KW_ACTIVE_POWER_DERATING_REGISTER, [0, Status.ON.value])
        print("Inverter reset to full power.")
    except Exception as e:
        msg = f"Could not reset inverter to full power: {e!r}"
        if notify_on_failure:
            log(msg)
        else:
            print(msg)  # expected at night when the inverter is off


def handle_shutdown_signal(signum, frame):
    # SystemExit is not caught by run_once's `except Exception`, so it unwinds to the finally below
    print(f"Received {signal.Signals(signum).name}, shutting down")
    raise SystemExit(0)


def main():
    while True:
        now = datetime.now(UTC)
        window_start, window_end = daylight_window(now)
        if now < window_start:
            sleep_until(window_start, "waiting for daylight")
            continue

        cycle_start = now
        try:
            run_once(window_end)
        except Exception as e:
            # run_once already handles its own errors; this catches failures in the fallback itself
            print(f"Unhandled error in cycle: {e!r}")

        next_run = cycle_start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1, seconds=RUN_OFFSET_SECONDS)
        if next_run >= window_end:
            sleep_until(window_end, "end of daylight")
            reset_to_normal(notify_on_failure=False)
        else:
            # based on cycle_start, so a 'match' cycle that overshoots the hour runs the next one immediately
            sleep_until(next_run, "next hourly cycle")


if __name__ == "__main__":
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, handle_shutdown_signal)
    try:
        main()
    finally:
        # runs on signals and on crashes alike (not on SIGKILL, which cannot be caught)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # don't let a second signal interrupt the reset
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        start, end = daylight_window(datetime.now(UTC))
        reset_to_normal(notify_on_failure=start <= datetime.now(UTC) < end)
