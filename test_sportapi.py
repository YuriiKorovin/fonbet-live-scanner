import os
import requests
from datetime import datetime, timezone

API_KEY = os.getenv("SPORTAPI_KEY")

if not API_KEY:
    print("ERROR: SPORTAPI_KEY не найден")
    raise SystemExit(1)

today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

url = f"https://sportapi.ai/api/fixtures/date/{today}"

headers = {
    "X-Api-Key": API_KEY,
    "Accept": "application/json"
}

try:
    response = requests.get(
        url,
        headers=headers,
        timeout=20
    )

    print("HTTP STATUS:", response.status_code)

    response.raise_for_status()
    result = response.json()

    matches = result.get("data", [])
    live = [
        match for match in matches
        if match.get("status") == "LIVE"
    ]

    print("Всего матчей:", len(matches))
    print("LIVE матчей:", len(live))

    for match in live:
        print(
            match.get("home_team"),
            "-",
            match.get("away_team"),
            "|",
            match.get("home_score"),
            ":",
            match.get("away_score"),
            "|",
            match.get("minute"),
            "мин."
        )

except Exception as error:
    print("ERROR:", str(error))