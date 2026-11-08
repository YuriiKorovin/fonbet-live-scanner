
import os
import time
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

API_KEY = os.getenv("SPORTAPI_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

API_BASE = "https://sportapi.ai/api"
CHECK_INTERVAL = 300
STATS_COOLDOWN = 600
MAX_STATS_REQUESTS_PER_CYCLE = 2

MINUTE_MIN = 60
MINUTE_MAX = 75
MIN_TOTAL_SHOTS = 15
MIN_DOMINANT_TEAM_SHOTS = 10
MIN_SHOTS_DIFFERENCE = 6
MIN_SHOTS_ON_TARGET = 3

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
session = requests.Session()
sent_matches = set()
last_stats_request = {}
stats_queue = []


def validate_environment():
    missing = [name for name, value in (
        ("SPORTAPI_KEY", API_KEY),
        ("TELEGRAM_BOT_TOKEN", BOT_TOKEN),
        ("TELEGRAM_CHANNEL_ID", CHANNEL_ID),
    ) if not value]
    if missing:
        raise RuntimeError("Не заданы переменные Railway: " + ", ".join(missing))


def api_get(url):
    response = session.get(
        url,
        headers={"X-Api-Key": API_KEY, "Accept": "application/json"},
        timeout=25,
    )
    if response.status_code == 429:
        raise RuntimeError("SportAPI.ai: достигнут лимит запросов (HTTP 429).")
    response.raise_for_status()
    payload = response.json()
    if payload.get("success") is False:
        raise RuntimeError(f"SportAPI.ai вернул ошибку: {payload}")
    return payload


def get_fixtures():
    # SportAPI.ai ведёт календарную дату по CET/CEST.
    today = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%Y-%m-%d")
    payload = api_get(f"{API_BASE}/fixtures/date/{today}")
    fixtures = payload.get("data", [])
    if not isinstance(fixtures, list):
        raise RuntimeError("Неожиданный формат списка матчей SportAPI.ai")
    return fixtures


def get_minute(value):
    if value is None:
        return None
    if isinstance(value, int):
        return value
    value = str(value).strip().replace("'", "")
    return int(value) if value.isdigit() else None


def is_candidate(match):
    minute = get_minute(match.get("minute"))
    try:
        home_score = int(match.get("home_score") or 0)
        away_score = int(match.get("away_score") or 0)
    except (TypeError, ValueError):
        return False
    return (
        str(match.get("status", "")).upper() == "LIVE"
        and minute is not None
        and MINUTE_MIN <= minute <= MINUTE_MAX
        and home_score == 0
        and away_score == 0
    )


def stat_value(team_data, key):
    value = team_data.get(key)
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def get_fixture_statistics(fixture_id):
    payload = api_get(f"{API_BASE}/fixtures/{fixture_id}/stats")
    if payload.get("available") is False:
        return None
    data = payload.get("data") or {}
    home = data.get("home") or {}
    away = data.get("away") or {}

    values = {
        "home_shots": stat_value(home, "shots"),
        "away_shots": stat_value(away, "shots"),
        "home_on_target": stat_value(home, "shots_on_target"),
        "away_on_target": stat_value(away, "shots_on_target"),
    }
    return None if any(value is None for value in values.values()) else values


def build_signal(match, stats):
    hs, aws = stats["home_shots"], stats["away_shots"]
    hst, ast = stats["home_on_target"], stats["away_on_target"]
    total_shots = hs + aws
    total_on_target = hst + ast

    if not (
        total_shots >= MIN_TOTAL_SHOTS
        and max(hs, aws) >= MIN_DOMINANT_TEAM_SHOTS
        and abs(hs - aws) >= MIN_SHOTS_DIFFERENCE
        and total_on_target >= MIN_SHOTS_ON_TARGET
    ):
        return None

    home = match.get("home_team") or "Хозяева"
    away = match.get("away_team") or "Гости"
    dominant = home if hs > aws else away
    minute = get_minute(match.get("minute"))
    league = match.get("league_name") or "Лига"

    return (
        "⚽ LIVE СИГНАЛ — СИЛЬНОЕ ПРЕИМУЩЕСТВО\n\n"
        f"{home} — {away}\nЛига: {league}\nСчёт: 0:0\nМинута: {minute}'\n\n"
        f"Всего ударов: {total_shots} ({hs} — {aws})\n"
        f"Удары в створ: {total_on_target} ({hst} — {ast})\n"
        f"Преимущество по ударам: {dominant}\n\n"
        "Источник статистики: SportAPI.ai"
    )


def send_telegram(message):
    response = session.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={"chat_id": CHANNEL_ID, "text": message, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Ошибка Telegram: {result}")


def check_matches():
    global stats_queue
    fixtures = get_fixtures()
    now = time.time()
    candidates = {}

    for match in fixtures:
        fixture_id = match.get("id")
        if fixture_id is not None:
            fixture_id = str(fixture_id)
            if fixture_id not in sent_matches and is_candidate(match):
                candidates[fixture_id] = match

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
            if not stats:
                logging.info("Статистика пока недоступна для матча %s", fixture_id)
                continue
            message = build_signal(candidates[fixture_id], stats)
            if message:
                send_telegram(message)
                sent_matches.add(fixture_id)
                stats_queue.remove(fixture_id)
                logging.info("Сигнал отправлен: %s", fixture_id)
            else:
                logging.info("Матч %s не прошёл фильтр", fixture_id)
        except Exception as error:
            logging.warning("Ошибка обработки матча %s: %s", fixture_id, error)


def main():
    validate_environment()
    logging.info("Fonbet Live Scanner запущен через SportAPI.ai")
    while True:
        try:
            check_matches()
        except Exception as error:
            logging.exception("Ошибка сканера: %s", error)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()