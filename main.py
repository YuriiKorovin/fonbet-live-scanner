
import os
import time
import logging
import requests

API_KEY = os.getenv("API_FOOTBALL_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

BASE_URL = "https://v3.football.api-sports.io"
LIVE_URL = f"{BASE_URL}/fixtures"
STATS_URL = f"{BASE_URL}/fixtures/statistics"

CHECK_INTERVAL = 120
STATS_COOLDOWN = 180
MAX_STATS_REQUESTS_PER_CYCLE = 3
QUOTA_PAUSE_SECONDS = 6 * 60 * 60

MINUTE_MIN = 60
MINUTE_MAX = 75
MIN_TOTAL_SHOTS = 15
MIN_DOMINANT_TEAM_SHOTS = 10
MIN_SHOTS_DIFFERENCE = 6
MIN_SHOTS_ON_TARGET = 3
MIN_CORNERS = 4

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
session = requests.Session()
sent_matches = set()
last_stats_request = {}
stats_queue = []


class DailyQuotaExceeded(RuntimeError):
    pass


def required_variables_present():
    missing = [name for name, value in (
        ("API_FOOTBALL_KEY", API_KEY),
        ("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
        ("TELEGRAM_CHANNEL_ID", CHANNEL_ID),
    ) if not value]
    if missing:
        raise RuntimeError("ÐÐµ Ð·Ð°Ð´Ð°Ð½Ñ Ð¿ÐµÑÐµÐ¼ÐµÐ½Ð½ÑÐµ Railway: " + ", ".join(missing))


def api_get(url, params):
    response = session.get(
        url, headers={"x-apisports-key": API_KEY},
        params=params, timeout=30
    )
    response.raise_for_status()
    data = response.json()
    if data.get("errors"):
        error_text = str(data["errors"])
        if "request limit for the day" in error_text.lower():
            raise DailyQuotaExceeded(error_text)
        raise RuntimeError(f"API-Football error: {error_text}")
    return data.get("response", [])


def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = session.post(
        url, json={"chat_id": CHANNEL_ID, "text": message}, timeout=20
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def get_stat_value(statistics, *names):
    wanted = {name.lower() for name in names}
    for stat in statistics or []:
        if str(stat.get("type", "")).strip().lower() in wanted:
            value = stat.get("value")
            if value is None:
                return None
            try:
                return int(float(str(value).replace("%", "").strip()))
            except (TypeError, ValueError):
                return None
    return None


def get_fixture_statistics(fixture_id):
    teams_data = api_get(STATS_URL, {"fixture": fixture_id})
    if len(teams_data) < 2:
        return None

    result = []
    for team_data in teams_data:
        values = team_data.get("statistics", [])
        shots = get_stat_value(values, "Total Shots")
        on_target = get_stat_value(values, "Shots on Goal", "Shots on Target")
        corners = get_stat_value(values, "Corner Kicks", "Corners")

        # ÐÐµ Ð¿Ð¾Ð´Ð¼ÐµÐ½ÑÐµÐ¼ Ð¾ÑÑÑÑÑÑÐ²ÑÑÑÑÑ ÑÑÐ°ÑÐ¸ÑÑÐ¸ÐºÑ Ð½ÑÐ»ÑÐ¼Ð¸.
        if shots is None or on_target is None or corners is None:
            return None

        result.append({
            "team": team_data.get("team", {}).get("name", "ÐÐ¾Ð¼Ð°Ð½Ð´Ð°"),
            "shots": shots,
            "on_target": on_target,
            "corners": corners,
        })
    return result


def is_candidate(item):
    fixture = item.get("fixture", {})
    status = fixture.get("status", {})
    minute = status.get("elapsed")
    goals = item.get("goals", {})
    return (
        status.get("short") == "2H"
        and minute is not None
        and MINUTE_MIN <= minute <= MINUTE_MAX
        and goals.get("home") == 0
        and goals.get("away") == 0
    )


def build_signal(item, stats):
    home, away = stats
    total_shots = home["shots"] + away["shots"]
    dominant_shots = max(home["shots"], away["shots"])
    shots_difference = abs(home["shots"] - away["shots"])
    total_on_target = home["on_target"] + away["on_target"]
    total_corners = home["corners"] + away["corners"]

    if not (
        total_shots >= MIN_TOTAL_SHOTS
        and dominant_shots >= MIN_DOMINANT_TEAM_SHOTS
        and shots_difference >= MIN_SHOTS_DIFFERENCE
        and total_on_target >= MIN_SHOTS_ON_TARGET
        and total_corners >= MIN_CORNERS
    ):
        return None

    teams = item.get("teams", {})
    home_name = teams.get("home", {}).get("name", home["team"])
    away_name = teams.get("away", {}).get("name", away["team"])
    minute = item.get("fixture", {}).get("status", {}).get("elapsed")

    return (
        "â½ LIVE Ð¡ÐÐÐÐÐ â Ð¡ÐÐÐ¬ÐÐÐ ÐÐ ÐÐÐÐ£Ð©ÐÐ¡Ð¢ÐÐ\n\n"
        f"{home_name} â {away_name}\n"
        "Ð¡ÑÑÑ: 0:0\n"
        f"ÐÐ¸Ð½ÑÑÐ°: {minute}'\n\n"
        f"ÐÑÐµÐ³Ð¾ ÑÐ´Ð°ÑÐ¾Ð²: {total_shots} ({home['shots']} â {away['shots']})\n"
        f"Ð£Ð´Ð°ÑÑ Ð² ÑÑÐ²Ð¾Ñ: {total_on_target} "
        f"({home['on_target']} â {away['on_target']})\n"
        f"Ð£Ð³Ð»Ð¾Ð²ÑÐµ: {total_corners} ({home['corners']} â {away['corners']})\n\n"
        "ÐÑÑÐ¾ÑÐ½Ð¸Ðº: API-Football"
    )


def check_matches():
    global stats_queue
    fixtures = api_get(LIVE_URL, {"live": "all"})
    now = time.time()

    candidates = {}
    for item in fixtures:
        fixture_id = str(item.get("fixture", {}).get("id", ""))
        if fixture_id and fixture_id not in sent_matches and is_candidate(item):
            candidates[fixture_id] = item

    for fixture_id in candidates:
        if fixture_id not in stats_queue:
            stats_queue.append(fixture_id)
    stats_queue = [fid for fid in stats_queue if fid in candidates]

    requests_made = 0
    for fixture_id in list(stats_queue):
        if requests_made >= MAX_STATS_REQUESTS_PER_CYCLE:
            break
        if now - last_stats_request.get(fixture_id, 0) < STATS_COOLDOWN:
            continue

        last_stats_request[fixture_id] = now
        requests_made += 1
        try:
            stats = get_fixture_statistics(fixture_id)
        except DailyQuotaExceeded:
            raise
        except Exception as error:
            logging.warning("ÐÑÐ¸Ð±ÐºÐ° ÑÑÐ°ÑÐ¸ÑÑÐ¸ÐºÐ¸ Ð¼Ð°ÑÑÐ° %s: %s", fixture_id, error)
            continue

        if not stats:
            logging.info("ÐÐµÑ Ð¿Ð¾Ð»Ð½Ð¾Ð¹ ÑÑÐ°ÑÐ¸ÑÑÐ¸ÐºÐ¸ Ð´Ð»Ñ Ð¼Ð°ÑÑÐ° %s", fixture_id)
            continue

        message = build_signal(candidates[fixture_id], stats)
        if message:
            send_telegram(message)
            sent_matches.add(fixture_id)
            stats_queue.remove(fixture_id)
            logging.info("Ð¡Ð¸Ð³Ð½Ð°Ð» Ð¾ÑÐ¿ÑÐ°Ð²Ð»ÐµÐ½: %s", fixture_id)
        else:
            logging.info("ÐÐ°ÑÑ %s Ð½Ðµ Ð¿ÑÐ¾ÑÑÐ» ÑÐ¸Ð»ÑÑÑ", fixture_id)


def main():
    required_variables_present()
    logging.info("Fonbet Live Scanner Ð·Ð°Ð¿ÑÑÐµÐ½")
    while True:
        try:
            check_matches()
        except DailyQuotaExceeded as error:
            logging.error("ÐÐ½ÐµÐ²Ð½Ð¾Ð¹ Ð»Ð¸Ð¼Ð¸Ñ API Ð¸ÑÑÐµÑÐ¿Ð°Ð½: %s", error)
            logging.error("ÐÐ°ÑÐ·Ð° Ð½Ð° 6 ÑÐ°ÑÐ¾Ð².")
            time.sleep(QUOTA_PAUSE_SECONDS)
        except requests.RequestException as error:
            logging.exception("Ð¡ÐµÑÐµÐ²Ð°Ñ Ð¾ÑÐ¸Ð±ÐºÐ°: %s", error)
            time.sleep(60)
        except Exception as error:
            logging.exception("ÐÑÐ¸Ð±ÐºÐ° ÑÐºÐ°Ð½ÐµÑÐ°: %s", error)
            time.sleep(60)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
