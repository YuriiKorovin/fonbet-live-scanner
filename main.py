
import os
import time
import requests

API_KEY = os.getenv("API_FOOTBALL_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

API_URL = "https://v3.football.api-sports.io/fixtures"
STATS_URL = "https://v3.football.api-sports.io/fixtures/statistics"

# Проверяем матчи раз в минуту, чтобы не пропускать окно 60–75-й минуты.
CHECK_INTERVAL = 60

MINUTE_MIN = 60
MINUTE_MAX = 75

MIN_TOTAL_SHOTS = 15
MIN_DOMINANT_TEAM_SHOTS = 10
MIN_SHOTS_DIFFERENCE = 6
MIN_SHOTS_ON_GOAL_TOTAL = 3
MIN_CORNERS_TOTAL = 4

sent_matches = set()


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": CHANNEL_ID, "text": message},
        timeout=20
    )
    response.raise_for_status()

    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def get_stat_value(statistics, stat_name):
    """Возвращает числовое значение статистики или None, если данных нет."""
    for stat in statistics:
        if stat.get("type") == stat_name:
            value = stat.get("value")
            if value is None:
                return None
            try:
                return int(str(value).replace("%", "").strip())
            except (TypeError, ValueError):
                return None
    return None


def get_fixture_statistics(headers, fixture_id):
    response = requests.get(
        STATS_URL,
        headers=headers,
        params={"fixture": fixture_id},
        timeout=25
    )
    response.raise_for_status()
    data = response.json()

    if data.get("errors"):
        raise RuntimeError(f"Statistics API error: {data['errors']}")

    teams_stats = data.get("response", [])
    if len(teams_stats) < 2:
        return None

    result = []
    for team_data in teams_stats:
        stats = team_data.get("statistics", [])
        result.append({
            "team": team_data.get("team", {}).get("name", "Команда"),
            "shots": get_stat_value(stats, "Total Shots"),
            "on_target": get_stat_value(stats, "Shots on Goal"),
            "corners": get_stat_value(stats, "Corner Kicks"),
        })

    # Если лига не предоставляет нужную статистику, матч пропускаем.
    for team in result:
        if any(team[key] is None for key in ("shots", "on_target", "corners")):
            return None

    return result


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
        raise RuntimeError(f"Fixtures API error: {data['errors']}")

    for item in data.get("response", []):
        fixture = item["fixture"]
        goals = item["goals"]
        teams = item["teams"]
        status = fixture["status"]

        fixture_id = str(fixture["id"])
        minute = status.get("elapsed")
        home_score = goals.get("home")
        away_score = goals.get("away")

        # Только второй тайм, 60–75-я минута, счёт 0:0.
        if not (
            status.get("short") == "2H"
            and minute is not None
            and MINUTE_MIN <= minute <= MINUTE_MAX
            and home_score == 0
            and away_score == 0
            and fixture_id not in sent_matches
        ):
            continue

        try:
            stats = get_fixture_statistics(headers, fixture_id)
        except Exception as error:
            print(f"Statistics error for fixture {fixture_id}: {error}")
            continue

        if not stats:
            print(f"No usable statistics for fixture {fixture_id}")
            continue

        home, away = stats[0], stats[1]

        total_shots = home["shots"] + away["shots"]
        dominant_shots = max(home["shots"], away["shots"])
        shots_difference = abs(home["shots"] - away["shots"])
        total_on_target = home["on_target"] + away["on_target"]
        total_corners = home["corners"] + away["corners"]

        # Одна команда должна явно доминировать по ударам.
        qualifies = (
            total_shots >= MIN_TOTAL_SHOTS
            and dominant_shots >= MIN_DOMINANT_TEAM_SHOTS
            and shots_difference >= MIN_SHOTS_DIFFERENCE
            and total_on_target >= MIN_SHOTS_ON_GOAL_TOTAL
            and total_corners >= MIN_CORNERS_TOTAL
        )

        if not qualifies:
            continue

        message = (
            "⚽ LIVE СИГНАЛ — СИЛЬНОЕ ПРЕИМУЩЕСТВО\n\n"
            f"{teams['home']['name']} — {teams['away']['name']}\n"
            "Счёт: 0:0\n"
            f"Минута: {minute}'\n\n"
            f"Всего ударов: {total_shots} "
            f"({home['shots']} — {away['shots']})\n"
            f"Удары в створ: {total_on_target} "
            f"({home['on_target']} — {away['on_target']})\n"
            f"Угловые: {total_corners} "
            f"({home['corners']} — {away['corners']})\n\n"
            "Источник: API-Football"
        )

        send_telegram(message)
        sent_matches.add(fixture_id)
        print(f"Signal sent: {fixture_id}")


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
