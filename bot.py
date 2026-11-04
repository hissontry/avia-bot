"""
Бот следит за ценами на авиабилеты Aviasales (через Travelpayouts Data API)
и пишет в Telegram-канал, когда цена на маршрут падает или растёт.

Запуск:
    python bot.py            — работать постоянно, проверяя цены по расписанию
    python bot.py --once     — проверить один раз и выйти (для планировщика задач)
    python bot.py --test     — отправить тестовое сообщение в канал
    python bot.py --dry-run  — проверить цены и напечатать сообщения в консоль, ничего не отправляя
    python bot.py --report   — проверить один раз и сразу отправить ежедневный отчёт

Нужны только стандартные библиотеки Python 3.8+.
"""

import argparse
import html
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

API_URL = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
SPECIAL_OFFERS_URL = "https://api.travelpayouts.com/aviasales/v3/get_special_offers"
CURRENCY_SIGNS = {"rub": "₽", "usd": "$", "eur": "€", "kzt": "₸", "uah": "₴", "byn": "Br"}


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": "price-watch-bot", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------- Aviasales / Travelpayouts ----------

def query_prices(route, cfg, departure_at, limit):
    params = {
        "origin": route["origin"].upper(),
        "destination": route["destination"].upper(),
        "departure_at": departure_at,
        "currency": cfg.get("currency", "rub"),
        "sorting": "price",
        "direct": "true" if route.get("direct") else "false",
        "limit": limit,
        "page": 1,
        "market": cfg.get("market", "ru"),
    }
    if route.get("return_date"):
        params["return_at"] = route["return_date"]
        params["one_way"] = "false"
    else:
        params["one_way"] = "true"

    url = API_URL + "?" + urllib.parse.urlencode(params)
    data = http_get_json(url, headers={"X-Access-Token": cfg["travelpayouts_token"]})
    if not data.get("success"):
        raise RuntimeError(f"Travelpayouts ответил ошибкой: {data}")
    return data.get("data") or []


def months_between(start, end):
    """'2026-11-20', '2027-01-05' -> ['2026-11', '2026-12', '2027-01']"""
    y, m = int(start[:4]), int(start[5:7])
    result = []
    while f"{y:04d}-{m:02d}" <= end[:7]:
        result.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return result


def fetch_cheapest(route, cfg):
    """Возвращает самый дешёвый билет по маршруту (dict) или None, если билетов нет."""
    if not route.get("depart_from"):
        tickets = query_prices(route, cfg, route["depart_date"], limit=1)
        return tickets[0] if tickets else None

    best = None
    for t in fetch_range(route, cfg):
        if best is None or t["price"] < best["price"]:
            best = t
    return best


def fetch_range(route, cfg):
    """Все билеты с вылетом в [depart_from, depart_to].

    API ищет только по дню или месяцу, поэтому берём каждый месяц диапазона
    и отбрасываем билеты с вылетом вне него.
    """
    start, end = route["depart_from"], route["depart_to"]
    result = []
    for month in months_between(start, end):
        for t in query_prices(route, cfg, month, limit=1000):
            day = (t.get("departure_at") or "")[:10]
            if start <= day <= end:
                result.append(t)
    return result


def cheapest_by_day(tickets):
    best = {}
    for t in tickets:
        day = t["departure_at"][:10]
        if day not in best or t["price"] < best[day]["price"]:
            best[day] = t
    return best


def route_key(route):
    depart = route.get("depart_date") or f"{route['depart_from']}..{route['depart_to']}"
    return "|".join([
        route["origin"].upper(),
        route["destination"].upper(),
        depart,
        route.get("return_date") or "-",
        "direct" if route.get("direct") else "any",
    ])


# ---------- Telegram ----------

def send_telegram(cfg, text):
    url = f"https://api.telegram.org/bot{cfg['telegram_bot_token']}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id": cfg["channel_id"],
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            details = e.read().decode("utf-8", "replace")
            if e.code == 429 and attempt < 2:
                retry = json.loads(details).get("parameters", {}).get("retry_after", 5)
                time.sleep(retry + 1)
                continue
            raise RuntimeError(f"Telegram {e.code}: {details}") from None


# ---------- Форматирование ----------

def fmt_money(value, cfg):
    sign = CURRENCY_SIGNS.get(cfg.get("currency", "rub").lower(), cfg.get("currency", "").upper())
    return f"{value:,.0f}".replace(",", " ") + f" {sign}"


def fmt_date(value):
    """'2026-12-20' / '2026-12' / '2026-12-20T10:15:00+03:00' -> человекочитаемо."""
    if not value:
        return ""
    try:
        if "T" in value:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.strftime("%d.%m.%Y %H:%M")
        if len(value) == 7:
            return datetime.strptime(value, "%Y-%m").strftime("%m.%Y (весь месяц)")
        return datetime.strptime(value, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return value


def route_title(route):
    name = route.get("name") or f"{route['origin'].upper()} → {route['destination'].upper()}"
    if route.get("depart_from"):
        dates = f"вылет с {fmt_date(route['depart_from'])} по {fmt_date(route['depart_to'])}"
    else:
        dates = fmt_date(route["depart_date"])
    if route.get("return_date"):
        dates += f" — {fmt_date(route['return_date'])}"
    else:
        dates += " (в одну сторону)"
    return f"<b>{html.escape(name)}</b>\n🗓 {dates}"


def ticket_details(ticket, cfg):
    lines = []
    if ticket.get("departure_at"):
        lines.append(f"🛫 Вылет: {fmt_date(ticket['departure_at'])}")
    if ticket.get("return_at"):
        lines.append(f"🛬 Обратно: {fmt_date(ticket['return_at'])}")
    transfers = ticket.get("transfers")
    if transfers is not None:
        lines.append("✈️ Прямой рейс" if transfers == 0 else f"🔁 Пересадок: {transfers}")
    if ticket.get("airline"):
        flight = f"{ticket['airline']} {ticket.get('flight_number', '')}".strip()
        lines.append(f"🏷 Рейс: {html.escape(flight)}")
    if ticket.get("link"):
        domain = cfg.get("aviasales_domain", "https://www.aviasales.ru")
        lines.append(f'🔗 <a href="{html.escape(domain + ticket["link"])}">Открыть на Aviasales</a>')
    return "\n".join(lines)


def build_message(route, old, new_ticket, cfg):
    """old — прошлая цена (число) или None при первом запуске."""
    title = route_title(route)
    if new_ticket is None:
        return f"🚫 Билеты пропали из выдачи\n{title}\n\nПоследняя известная цена: {fmt_money(old, cfg)}"

    price = new_ticket["price"]
    details = ticket_details(new_ticket, cfg)
    if old is None:
        return f"👀 Начинаю следить за ценой\n{title}\n\n💰 Сейчас: <b>{fmt_money(price, cfg)}</b>\n{details}"

    diff = price - old
    pct = diff / old * 100 if old else 0
    if diff < 0:
        head = "📉 <b>Цена упала!</b>"
    else:
        head = "📈 <b>Цена выросла</b>"
    sign = "+" if diff > 0 else "−"
    change = f"{sign}{fmt_money(abs(diff), cfg)} ({sign}{abs(pct):.1f}%)"
    return (
        f"{head}\n{title}\n\n"
        f"💰 Было: {fmt_money(old, cfg)} → Стало: <b>{fmt_money(price, cfg)}</b>\n"
        f"Δ {change}\n{details}"
    )


def build_hot_message(route, ticket, usual, cfg):
    name = route.get("name") or f"{route['origin'].upper()} → {route['destination'].upper()}"
    price_line = f"💰 <b>{fmt_money(ticket['price'], cfg)}</b>"
    if usual and ticket["price"] < usual:
        off = (1 - ticket["price"] / usual) * 100
        usual_rounded = round(usual / 100) * 100
        price_line += f" вместо обычных ~{fmt_money(usual_rounded, cfg)} (−{off:.0f}%)"
    return (
        f"🔥 <b>Горячее предложение!</b>\n<b>{html.escape(name)}</b>\n\n"
        f"{price_line}\n{ticket_details(ticket, cfg)}"
    )


# ---------- Основная логика ----------

def record_error(state, where, error):
    """Запоминает ошибку для ежедневного отчёта."""
    errors = state.setdefault("_errors", [])
    errors.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "where": where, "text": str(error)[:200]})
    del errors[:-30]


def publish(cfg, message, dry_run, key, state):
    """Отправляет сообщение в канал. Возвращает True, если получилось."""
    if dry_run:
        print("\n----- сообщение -----\n" + message + "\n---------------------\n")
        return True
    try:
        send_telegram(cfg, message)
        log(f"{key}: отправлено в канал")
        return True
    except Exception as e:
        log(f"{key}: не удалось отправить в Telegram — {e}")
        record_error(state, "Отправка в Telegram", e)
        return False


def fetch_special_offers(route, cfg):
    """Спецпредложения Aviasales — билеты с аномально низкой ценой («Горячее предложение»)."""
    params = {
        "origin": route["origin"].upper(),
        "destination": route["destination"].upper(),
        "currency": cfg.get("currency", "rub"),
        "market": cfg.get("market", "ru"),
        "locale": "ru",
    }
    url = SPECIAL_OFFERS_URL + "?" + urllib.parse.urlencode(params)
    data = http_get_json(url, headers={"X-Access-Token": cfg["travelpayouts_token"]})
    items = data.get("data") or []
    if isinstance(items, dict):
        items = list(items.values())
    return [t for t in items if isinstance(t, dict) and t.get("price") and t.get("departure_at")]


def offer_key(ticket):
    return f"{ticket['departure_at'][:10]}|{ticket.get('airline', '')}{ticket.get('flight_number', '')}"


def check_hot_deals(cfg, state, dry_run=False):
    """Отправляет все горячие предложения с вылетом в [depart_from, depart_to].

    Горячим считается билет из спецпредложений Aviasales, а также день, когда
    самая дешёвая цена ниже обычной (медиана по дням диапазона) на discount_percent.
    """
    min_change = cfg.get("min_change", 0)
    today = datetime.now().strftime("%Y-%m-%d")
    hot_total = 0

    for route in cfg.get("hot_deals", []):
        key = "hot|" + route_key(route)
        start, end = max(route["depart_from"], today), route["depart_to"]
        candidates = {}
        specials_ok = False

        try:
            specials = fetch_special_offers(route, cfg)
            specials_ok = True
            for t in specials:
                if start <= t["departure_at"][:10] <= end:
                    candidates[offer_key(t)] = t
            log(f"{key}: спецпредложений Aviasales {len(specials)}, в нужные даты {len(candidates)}")
            if specials:
                log(f"{key}: пример спецпредложения {json.dumps(specials[0], ensure_ascii=False)[:400]}")
        except Exception as e:
            log(f"{key}: спецпредложения недоступны — {e}")
            record_error(state, "Спецпредложения Aviasales", e)

        try:
            days = cheapest_by_day(fetch_range(route, cfg))
        except Exception as e:
            log(f"{key}: ошибка запроса цен — {e}")
            record_error(state, "Горячие предложения", e)
            days = {}
        days = {d: t for d, t in days.items() if d >= start}
        pct = route.get("discount_percent", 30)
        usual = statistics.median(t["price"] for t in days.values()) if len(days) >= 5 else None
        if usual:
            for t in days.values():
                if t["price"] <= usual * (1 - pct / 100):
                    candidates.setdefault(offer_key(t), t)

        seen = state.get(key, {})  # предложение -> цена, о которой уже написали
        new_seen = {}
        sent = 0
        for k, t in sorted(candidates.items(), key=lambda kv: kv[1]["price"]):
            # Старые записи хранились по одному дню без номера рейса
            prev = seen.get(k, seen.get(k[:10]))
            if prev is not None and prev - t["price"] < min_change:
                new_seen[k] = prev  # уже писали, и сильнее не подешевело
                continue
            if sent < 20 and publish(cfg, build_hot_message(route, t, usual, cfg), dry_run, key, state):
                new_seen[k] = t["price"]
                sent += 1
            elif prev is not None:
                new_seen[k] = prev
        # Предложение, которое чуть-чуть перестало быть горячим, не забываем сразу,
        # чтобы не писать о нём повторно при колебаниях цены у порога.
        # Если спецпредложения сейчас не ответили, тоже ничего не забываем.
        handled_days = {k[:10] for k in new_seen}
        for k, price in seen.items():
            day = k[:10]
            near_threshold = (usual and day in days
                              and days[day]["price"] <= usual * (1 - pct / 200))
            if day not in handled_days and (near_threshold or not specials_ok) and day >= start:
                new_seen[k] = price
        state[key] = new_seen
        hot_total += len(candidates)
        usual_text = f"{usual:.0f}" if usual else "нет данных"
        log(f"{key}: обычная цена {usual_text}, горячих {len(candidates)}, отправлено {sent}")
        time.sleep(1)
    return hot_total


# ---------- Ежедневный отчёт ----------

def kyiv_tz():
    try:
        from zoneinfo import ZoneInfo
        for name in ("Europe/Kyiv", "Europe/Kiev"):
            try:
                return ZoneInfo(name)
            except Exception:
                pass
    except ImportError:
        pass
    return timezone(timedelta(hours=2))  # зимнее время Киева, если нет базы часовых поясов


def github_run_stats():
    """(проверок за сутки, из них упало) по данным GitHub Actions или None вне GitHub."""
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    current = os.environ.get("GITHUB_RUN_ID")
    total = failed = 0
    for page in range(1, 6):
        query = urllib.parse.urlencode({"created": f">={since}", "per_page": 100, "page": page})
        data = http_get_json(f"https://api.github.com/repos/{repo}/actions/runs?{query}", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
        runs = data.get("workflow_runs") or []
        for run in runs:
            if str(run.get("id")) == current or run.get("status") != "completed":
                continue
            total += 1
            if run.get("conclusion") in ("failure", "timed_out", "startup_failure"):
                failed += 1
        if len(runs) < 100:
            break
    return total, failed


def build_report(cfg, state, prices, hot_total, stats, now):
    errors = state.get("_errors", [])
    problems = bool(errors)
    lines = []

    if stats:
        total, failed = stats
        line = f"📊 За сутки: {total} проверок"
        line += f", упало {failed}" if failed else ", сбоев нет"
        lines.append(line)
        if failed:
            problems = True
        if total < cfg.get("daily_min_checks", 100):
            problems = True
            lines.append("⏰ Проверок меньше обычного (должно быть около 144). "
                         "Возможно, cron-job.org перестал запускать бота.")

    expires = cfg.get("cron_key_expires")
    if expires:
        days_left = (datetime.strptime(expires, "%Y-%m-%d").date() - now.date()).days
        if days_left <= 7:
            problems = True
            lines.append(f"🔑 Ключ для cron-job.org истекает {fmt_date(expires)} "
                         f"(осталось дней: {max(days_left, 0)}). Его нужно перевыпустить.")

    if errors:
        grouped = {}
        for e in errors:
            grouped.setdefault((e["where"], e["text"]), []).append(e["at"])
        lines.append(f"❗ Ошибки за сутки: {len(errors)}")
        for (where, text), times in list(grouped.items())[-5:]:
            last = datetime.fromisoformat(times[-1]).astimezone(now.tzinfo).strftime("%d.%m %H:%M")
            count = f" (×{len(times)})" if len(times) > 1 else ""
            lines.append(f"• {last} — {html.escape(where)}: {html.escape(text)}{count}")

    lines.append("\n💰 Цены сейчас:")
    for name, price in prices:
        value = fmt_money(price, cfg) if isinstance(price, int) else f"❌ {price}"
        lines.append(f"• {html.escape(name)}: {value}")
    if hot_total is not None and cfg.get("hot_deals"):
        lines.append(f"🔥 Горячих предложений в твои даты: {hot_total}")

    head = "⚠️ <b>Бот работает, но есть проблемы</b>" if problems else "✅ <b>Бот работает, всё гуд</b>"
    return f"{head}\n🕙 {now:%d.%m.%Y, %H:%M} по Киеву\n\n" + "\n".join(lines)


def maybe_daily_report(cfg, state, prices, hot_total, dry_run=False, force=False):
    hour = cfg.get("daily_report_hour")
    if hour is None and not force:
        return
    now = datetime.now(kyiv_tz())
    today = now.strftime("%Y-%m-%d")
    if not force and (now.hour < hour or state.get("_last_report") == today):
        return
    try:
        stats = github_run_stats()
    except Exception as e:
        log(f"Не удалось получить статистику запусков GitHub — {e}")
        stats = None
    if publish(cfg, build_report(cfg, state, prices, hot_total, stats, now), dry_run, "Ежедневный отчёт", state):
        state["_last_report"] = today
        state["_errors"] = []


def route_label(route):
    return route.get("name") or f"{route['origin'].upper()} → {route['destination'].upper()}"


def check_all(cfg, state, dry_run=False, force_report=False):
    min_change = cfg.get("min_change", 0)
    min_change_pct = cfg.get("min_change_percent", 0)
    prices = []  # для ежедневного отчёта: (маршрут, цена или текст проблемы)

    for route in cfg["routes"]:
        key = route_key(route)
        prev = state.get(key)  # {"price": ..., "available": bool}
        try:
            ticket = fetch_cheapest(route, cfg)
        except Exception as e:
            log(f"{key}: ошибка запроса цен — {e}")
            record_error(state, route_label(route), e)
            prices.append((route_label(route), "не удалось получить цену"))
            continue
        prices.append((route_label(route), ticket["price"] if ticket else "билетов нет"))

        message = None
        if ticket is None:
            log(f"{key}: билетов не найдено")
            if prev and prev.get("available"):
                message = build_message(route, prev["price"], None, cfg)
                state[key] = {**prev, "available": False}
        else:
            price = ticket["price"]
            log(f"{key}: {price}")
            if prev is None:
                if cfg.get("notify_on_start", True):
                    message = build_message(route, None, ticket, cfg)
                state[key] = {"price": price, "available": True}
            elif not prev.get("available") or price != prev["price"]:
                diff = abs(price - prev["price"])
                pct = diff / prev["price"] * 100 if prev["price"] else 100
                big_enough = diff >= min_change and pct >= min_change_pct
                if not prev.get("available") or (diff > 0 and big_enough):
                    message = build_message(route, prev["price"], ticket, cfg)
                    state[key] = {"price": price, "available": True}

        if message and not publish(cfg, message, dry_run, key, state):
            # Не сохраняем новое состояние, чтобы повторить отправку в следующий раз
            if prev is None:
                state.pop(key, None)
            else:
                state[key] = prev
        time.sleep(1)  # не долбим API слишком часто

    hot_total = check_hot_deals(cfg, state, dry_run)
    maybe_daily_report(cfg, state, prices, hot_total, dry_run, force_report)

    if not dry_run:
        save_json(STATE_PATH, state)


def validate_config(cfg):
    required = ["telegram_bot_token", "channel_id", "travelpayouts_token", "routes"]
    missing = [k for k in required if not cfg.get(k) or str(cfg.get(k)).startswith("ВСТАВЬ")]
    if missing:
        sys.exit(f"Заполни в config.json: {', '.join(missing)}")
    for r in cfg["routes"]:
        for k in ("origin", "destination"):
            if not r.get(k):
                sys.exit(f"В маршруте {r} не указано поле '{k}'")
        if not r.get("depart_date") and not (r.get("depart_from") and r.get("depart_to")):
            sys.exit(f"В маршруте {r} укажи 'depart_date' или пару 'depart_from' + 'depart_to'")
    for r in cfg.get("hot_deals", []):
        for k in ("origin", "destination", "depart_from", "depart_to"):
            if not r.get(k):
                sys.exit(f"В горячих предложениях {r} не указано поле '{k}'")


def main():
    parser = argparse.ArgumentParser(description="Бот слежения за ценами Aviasales")
    parser.add_argument("--once", action="store_true", help="проверить один раз и выйти")
    parser.add_argument("--test", action="store_true", help="отправить тестовое сообщение в канал")
    parser.add_argument("--dry-run", action="store_true", help="ничего не отправлять, только показать")
    parser.add_argument("--report", action="store_true", help="проверить и сразу отправить ежедневный отчёт")
    args = parser.parse_args()

    if not os.path.exists(CONFIG_PATH):
        sys.exit("Нет config.json — скопируй config.example.json в config.json и заполни его.")
    cfg = load_json(CONFIG_PATH, {})
    # Секреты можно передать через переменные окружения (так делает GitHub Actions)
    for key, env in (("telegram_bot_token", "TELEGRAM_BOT_TOKEN"),
                     ("channel_id", "TELEGRAM_CHANNEL_ID"),
                     ("travelpayouts_token", "TRAVELPAYOUTS_TOKEN")):
        if os.environ.get(env):
            cfg[key] = os.environ[env].strip()
    validate_config(cfg)

    if args.test:
        send_telegram(cfg, "✅ Бот подключён к каналу и готов следить за ценами.")
        log("Тестовое сообщение отправлено")
        return

    state = load_json(STATE_PATH, {})
    if args.once or args.dry_run or args.report:
        check_all(cfg, state, dry_run=args.dry_run, force_report=args.report)
        return

    interval = max(5, int(cfg.get("check_interval_minutes", 30))) * 60
    log(f"Бот запущен. Маршрутов: {len(cfg['routes'])}, проверка каждые {interval // 60} мин.")
    while True:
        check_all(cfg, state)
        time.sleep(interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Остановлено")
