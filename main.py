
import os
import time
import requests

API_KEY = os.getenv("API_FOOTBALL_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

API_URL = "https://v3.football.api-sports.io/fixtures"
CHECK_INTERVAL = 900  # 15 минут

sent_matches = set()

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": CHANNEL_ID, "text": text},
        timeout=20
    )
    response.raise_for_status()

def check_matches():
    headers = {"x-apisports-key": API_KEY}

    response = requests.get(
        API_URL,
        headers=headers,
        params={"live": "all"},
        timeout=30
    )
    response.raise_for_status()
    data = response.json()

    if data.get("errors"):
        raise RuntimeError(str(data["errors"]))

    for item in data.get("response", []):
        fixture = item["fixture"]
        goals = item["goals"]
        teams = item["teams"]
        status = fixture["status"]

        match_id = str(fixture["id"])
        minute = status.get("elapsed")
        home_score = goals.get("home")
        away_score = goals.get("away")

        if (
            status.get("short") in ("1H", "2H", "ET")
            and minute is not None
            and minute >= 60
            and home_score == 0
            and away_score == 0
            and match_id not in sent_matches
        ):
            message = (
                "⚽ LIVE СИГНАЛ\n\n"
                f"{teams['home']['name']} — "
                f"{teams['away']['name']}\n"
                "Счёт: 0:0\n"
                f"Минута: {minute}'\n\n"
                "Источник: API-Football"
            )

            send_telegram(message)
            sent_matches.add(match_id)
            print(f"Signal sent: {match_id}")

def main():
    if not all([API_KEY, BOT_TOKEN, CHANNEL_ID]):
        raise RuntimeError("Missing environment variables")

    print("Fonbet Live Scanner started")

    while True:
        try:
            check_matches()
        except Exception as error:
            print(f"Error: {error}")

        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
