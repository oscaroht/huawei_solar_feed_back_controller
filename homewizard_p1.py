import requests
import os

P1_IP = os.environ.get("P1_IP", "")
if not P1_IP:
    raise Exception("P1_IP not specified.")

def get_p1_data() -> dict:
    url = f"http://{P1_IP}/api/v1/data"
    r = requests.get(url)
    return r.json()
    


