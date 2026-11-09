
import os
import time
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

# Railway → Variables: SPORTAPI_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID
API_KEY = os.getenv("SPORTAPI_KEY")
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")

API_BASE = "https://sportapi.ai/api"
CHECK_INTERVAL = 600                 # список матчей — раз в 10 минут
STATS_COOLDOWN = 900                 # повтор статистики матча — не чаще 15 минут
MAX_STATS_REQUESTS_PER_DAY = 70      # запас по месячному лимиту даже при 7 000/мес
MAX_STATS_REQUESTS_PER_CYCLE = 1
QUEUE_MAX_AGE = 20 * 60               # не держим старые матчи в очереди дольше 20 минут

MINUTE_MIN = 55
MINUTE_MAX = 75
MIN_TOTAL_SHOTS = 10
MIN_DOMINANT_TEAM_SHOTS = 7
MIN_SHOTS_DIFFERENCE = 4
MIN_SHOTS_ON_TARGET = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
)
log = logging.getLogger("fonbet_live_scanner")
session = requests.Session()
sent_matches = set()
last_stats_request = {}
stats_queue = {}  # fixture_id -> {match, first_seen}
stats_requests_day = 0
stats_day = datetime.now(timezone.utc).date()


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
        timeout=(10, 25),
    )
    if response.status_code == 401:
        raise RuntimeError("SportAPI.ai: ключ SPORTAPI_KEY неверный или не активен (HTTP 401).")
    if response.status_code == 403:
        raise RuntimeError("SportAPI.ai отклонил доступ (HTTP 403). Проверьте статус ключа и доступ аккаунта.")
    if response.status_code == 429:
        raise RuntimeError("SportAPI.ai: достигнут лимит запросов (HTTP 429). Уменьшаем частоту запросов.")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("SportAPI.ai вернул неожиданный формат JSON.")
    if payload.get("success") is False:
        raise RuntimeError("SportAPI.ai вернул ошибку: " + str(payload.get("error", payload)))
    return payload


def get_fixtures():
    # Календарь SportAPI.ai использует CET/CEST.
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


def is_zero_zero_live(match):
    try:
        home_score = int(match.get("home_score") or 0)
        away_score = int(match.get("away_score") or 0)
    except (TypeError, ValueError):
        return False
    return str(match.get("status", "")).upper() == "LIVE" and home_score == 0 and away_score == 0


def is_candidate(match):
    minute = get_minute(match.get("minute"))
    return is_zero_zero_live(match) and minute is not None and MINUTE_MIN <= minute <= MINUTE_MAX


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
    if any(value is None for value in values.values()):
        return None
    return values


def build_signal(match, stats):
    minute = get_minute(match.get("minute"))
    # Не отправляем запоздавший сигнал, если матч уже вышел из заданного окна.
    if minute is None or not MINUTE_MIN <= minute <= MINUTE_MAX or not is_zero_zero_live(match):
        return None

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
    league = match.get("league_name") or "Лига не указана"
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
        timeout=(10, 20),
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError("Telegram вернул ошибку: " + str(result))


def reset_daily_stats_counter_if_needed():
    global stats_requests_day, stats_day
    today_utc = datetime.now(timezone.utc).date()
    if today_utc != stats_day:
        stats_day = today_utc
        stats_requests_day = 0
        log.info("Счётчик запросов статистики сброшен на новый день UTC.")


def check_matches():
    global stats_requests_day
    reset_daily_stats_counter_if_needed()

    fixtures = get_fixtures()
    now = time.time()
    candidates = {}
    live_zero_zero = 0

    for match in fixtures:
        if is_zero_zero_live(match):
            live_zero_zero += 1
        fixture_id = match.get("id")
        if fixture_id is None:
            continue
        fixture_id = str(fixture_id)
        if fixture_id not in sent_matches and is_candidate(match):
            candidates[fixture_id] = match
            if fixture_id not in stats_queue:
                stats_queue[fixture_id] = {"match": match, "first_seen": now}
            else:
                stats_queue[fixture_id]["match"] = match

    # Удаляем старые матчи и те, что уже не подходят по счёту/статусу/минуте.
    for fixture_id in list(stats_queue):
        item = stats_queue[fixture_id]
        match = item["match"]
        minute = get_minute(match.get("minute"))
        if (
            fixture_id not in candidates
            or now - item["first_seen"] > QUEUE_MAX_AGE
            or not is_zero_zero_live(match)
            or minute is None
            or minute > MINUTE_MAX
        ):
            stats_queue.pop(fixture_id, None)

    log.info(
        "Цикл проверки: матчей в календаре=%d; LIVE 0:0=%d; кандидатов 60–75 мин=%d; в очереди статистики=%d; запросов статистики сегодня=%d/%d",
        len(fixtures), live_zero_zero, len(candidates), len(stats_queue),
        stats_requests_day, MAX_STATS_REQUESTS_PER_DAY,
    )

    if stats_requests_day >= MAX_STATS_REQUESTS_PER_DAY:
        log.warning("Достигнут внутренний дневной лимит запросов статистики. До UTC-сброса статистику не запрашиваем.")
        return

    requests_made = 0
    for fixture_id in list(stats_queue):
        if requests_made >= MAX_STATS_REQUESTS_PER_CYCLE:
            break
        if now - last_stats_request.get(fixture_id, 0) < STATS_COOLDOWN:
            continue

        match = stats_queue[fixture_id]["match"]
        last_stats_request[fixture_id] = now
        requests_made += 1
        stats_requests_day += 1
        try:
            stats = get_fixture_statistics(fixture_id)
            if not stats:
                log.info("Статистика пока недоступна для матча %s; попробуем позже, если он останется в окне.", fixture_id)
                continue

            message = build_signal(match, stats)
            if message:
                send_telegram(message)
                sent_matches.add(fixture_id)
                stats_queue.pop(fixture_id, None)
                log.info("Сигнал успешно отправлен в Telegram: %s", fixture_id)
            else:
                log.info(
                    "Матч %s не прошёл фильтр: удары %s–%s, в створ %s–%s.",
                    fixture_id, stats["home_shots"], stats["away_shots"],
                    stats["home_on_target"], stats["away_on_target"],
                )
                stats_queue.pop(fixture_id, None)
        except requests.RequestException as error:
            log.exception("Сетевая ошибка при обработке матча %s: %s", fixture_id, error)
        except Exception as error:
            log.exception("Ошибка обработки матча %s: %s", fixture_id, error)


def main():
    validate_environment()
    log.info("Fonbet Live Scanner запущен через SportAPI.ai. Интервал=%d сек.", CHECK_INTERVAL)
    while True:
        started = time.monotonic()
        try:
            check_matches()
        except Exception as error:
            log.exception("Ошибка цикла сканера: %s", error)
        elapsed = time.monotonic() - started
        sleep_for = max(5, CHECK_INTERVAL - int(elapsed))
        log.info("Следующая проверка через %d сек.", sleep_for)
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()