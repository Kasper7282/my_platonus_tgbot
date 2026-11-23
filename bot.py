"""Telegram-бот: средние баллы из Platonus (tvpk.platonus.kz).

Команды:
  /grades                - средние баллы + расписание на сегодня
  /grades 1..5           - расписание на день недели (1 - понедельник ... 5 - пятница) с оценками
  /grades год семестр    - средние баллы за другой семестр (по умолчанию из .env)
  /today                 - оценки, выставленные сегодня
  /relogin               - принудительно войти в Platonus заново
  /teachers [on|off]     - показывать/скрывать ФИ преподавателей (без аргумента - переключить)
"""
# [IMPORTS] Библиотеки: asyncio уводит сетевые запросы из event loop;
# requests обслуживает Platonus; python-telegram-bot принимает команды.
import asyncio
import difflib
import html
import json
import logging
import os
import re
import sys
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from telegram import Bot, Update
from telegram.error import NetworkError, TimedOut
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, ContextTypes

# [STARTUP: ENV] Загружаем .env до чтения обязательных параметров ниже.
load_dotenv()

def required_env(name: str) -> str:
    """Возвращает обязательную переменную окружения или выдаёт понятную ошибку."""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Не задана переменная {name}. Добавьте её в .env рядом с bot.py."
        )
    return value


# [CONFIG: ENV] Секреты и параметры семестра читаются один раз при старте.
BOT_TOKEN = required_env("BOT_TOKEN")
PLATONUS_LOGIN = required_env("PLATONUS_LOGIN")
PLATONUS_PASSWORD = required_env("PLATONUS_PASSWORD")
# Telegram ID пользователей, которым разрешён доступ к боту.
_allowed_user_ids = os.environ.get("ALLOWED_USER_IDS", "").strip()
if not _allowed_user_ids:
    # Поддерживаем старое имя переменной для совместимости.
    _allowed_user_ids = required_env("ALLOWED_USER_ID")
try:
    ALLOWED_USER_IDS = {
        int(user_id.strip())
        for user_id in _allowed_user_ids.split(",")
        if user_id.strip()
    }
    if not ALLOWED_USER_IDS:
        raise ValueError("список пуст")
except ValueError as e:
    raise RuntimeError("ALLOWED_USER_IDS должен содержать ID через запятую.") from e
DEFAULT_YEAR = os.environ.get("PLATONUS_YEAR", "2026")
DEFAULT_TERM = os.environ.get("PLATONUS_TERM", "1")

# Какое значение из exams брать как «средний балл».
# Ищем по имени; если такого нет - берём первый элемент exams.
AVG_EXAM_NAME = "Ср.тек."

# [CONFIG: API] Адрес и заголовки, имитирующие запросы веб-клиента Platonus.
BASE = "https://tvpk.platonus.kz"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:151.0) Gecko/20100101 Firefox/151.0",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "language": "1",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": BASE,
    "Referer": BASE + "/",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("platonus-bot")


# [PLATONUS: SESSION] Отдельный сигнал для ответа, означающего истёкшую сессию.
class SessionExpired(Exception):
    pass


class Platonus:
    """Платонус-клиент: логин, хранение сессии, запрос журнала с авто-перелогином."""

    # [PLATONUS: CLIENT] Создаём HTTP-сессию и блокировку от параллельных логинов.
    def __init__(self, login: str, password: str):
        self.login = login
        self.password = password
        self.s = requests.Session()
        self.person_id: int | None = None
        self.lock = threading.Lock()

    # --- авторизация -------------------------------------------------------
    def authorize(self) -> None:
        """Получает новую сессию, personID и заголовки авторизации."""
        # Чистая сессия: старые куки не должны мешать
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self.s.headers["Content-Type"] = "application/json; charset=UTF-8"

        self.s.get(BASE + "/", timeout=20).raise_for_status()  # получаем JSESSIONID
        r = self.s.post(
            BASE + "/rest/api/login",
            json={"login": self.login, "iin": None, "password": self.password},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("login_status") != "success":
            raise RuntimeError(f"Платонус отклонил вход: {data}")

        self.person_id = data["personID"]
        # В запросах журнала браузер шлёт именно эти два заголовка
        self.s.headers["token"] = data["auth_token"]
        self.s.headers["sid"] = data["sid"]
        self.s.headers.pop("Content-Type", None)
        log.info("Вход в Платонус выполнен, personID=%s", self.person_id)

    # --- запросы -----------------------------------------------------------
    def _fetch_journal(self, year: str, term: str):
        """Запрашивает журнал; распознаёт отказ авторизации и HTML вместо JSON."""
        r = self.s.get(f"{BASE}/journal/{year}/{term}/{self.person_id}", timeout=20)
        if r.status_code in (401, 403):
            raise SessionExpired(f"HTTP {r.status_code}")
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            # вместо JSON пришла HTML-страница логина - сессия слетела
            raise SessionExpired("ответ не JSON")

    def journal(self, year: str, term: str):
        """Возвращает журнал, при истёкшей сессии выполняет один повторный вход."""
        with self.lock:
            if self.person_id is None:
                self.authorize()
            try:
                return self._fetch_journal(year, term)
            except SessionExpired as e:
                log.warning("Сессия слетела (%s), перелогиниваюсь", e)
                self.authorize()
                return self._fetch_journal(year, term)  # вторая попытка, без цикла

    def marks_for_date(self, year: str, term: str, target_date, subject_names=None):
        """Собирает записи за дату; при subject_names запрашивает только эти предметы."""
        with self.lock:
            if self.person_id is None:
                self.authorize()

            subjects = find_subjects(self._fetch_journal(year, term))
            requested = None
            if subject_names:
                requested = {norm(name) for name in subject_names}
                requested.update(ALIASES.get(key, key) for key in tuple(requested))
            if requested:
                subjects = [
                    subject for subject in subjects
                    if norm(subject.get("subjectName", "")) in requested
                    or any(
                        wanted in norm(subject.get("subjectName", ""))
                        or norm(subject.get("subjectName", "")) in wanted
                        for wanted in requested
                    )
                ]
            result = []
            for subject in subjects:
                subject_id = subject.get("subjectID")
                query_id = subject.get("queryID")
                if not subject_id or not query_id:
                    continue

                response = self.s.get(
                    f"{BASE}/subject/{year}/{term}/{subject_id}/{self.person_id}",
                    params={"queryID": query_id},
                    timeout=20,
                )
                if response.status_code in (401, 403):
                    raise SessionExpired(f"HTTP {response.status_code}")
                response.raise_for_status()
                groups = response.json()
                if not isinstance(groups, list):
                    continue

                for group in groups:
                    months = group.get("Marks", {}) if isinstance(group, dict) else {}
                    for month_data in months.values():
                        days = month_data.get("Marks", {}) if isinstance(month_data, dict) else {}
                        for entries in days.values():
                            if not isinstance(entries, list):
                                continue
                            for mark in entries:
                                stamp = mark.get("MarkDate", {})
                                try:
                                    mark_date = datetime(
                                        int(stamp["Year"]), int(stamp["Month"]), int(stamp["Day"])
                                    ).date()
                                except (KeyError, TypeError, ValueError):
                                    continue
                                if mark_date == target_date:
                                    result.append({
                                        "subject": subject.get("subjectName", group.get("Name", "?")),
                                        "mark": mark.get("Mark"),
                                        "symbol": mark.get("Symbol", ""),
                                    })
            return result

    def relogin(self) -> None:
        """Принудительно пересоздаёт авторизованную сессию Platonus."""
        with self.lock:
            self.authorize()


# [GRADES: PARSE] Преобразование вложенного ответа Platonus в предметы и баллы.
def find_subjects(data) -> list[dict]:
    """Ищет список предметов (объекты с subjectName) на любой глубине ответа."""
    if isinstance(data, list):
        if data and all(isinstance(x, dict) and "subjectName" in x for x in data):
            return data
        for x in data:
            found = find_subjects(x)
            if found:
                return found
    elif isinstance(data, dict):
        for v in data.values():
            found = find_subjects(v)
            if found:
                return found
    return []


def average_of(subject: dict) -> float | None:
    """Средний балл как число или None, если данных нет."""
    exams = subject.get("exams") or []
    chosen = next((e for e in exams if e.get("name") == AVG_EXAM_NAME), None)
    if chosen is None and exams:
        chosen = exams[0]
    if chosen is None:
        return None
    try:
        return float(chosen["mark"])
    except (KeyError, TypeError, ValueError):
        return None


# [SETTINGS: FILE] Переключатель показа преподавателей хранится между запусками.
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")


def load_settings() -> dict:
    """Читает настройки JSON; при отсутствии или повреждении использует значения по умолчанию."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(settings: dict) -> None:
    """Сохраняет настройки в UTF-8 JSON рядом с bot.py."""
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False)


def show_teachers() -> bool:
    """Возвращает текущий флаг отображения имён преподавателей."""
    return load_settings().get("show_teachers", True)


# [GRADES: DISPLAY] Порог цветной отметки; менять диапазоны нужно в mark_emoji.
PASS_MARK = 90  # верхняя граница оранжевого диапазона


def mark_emoji(avg: float | None) -> str:
    """Преобразует средний балл в цветной круг (⚪ для отсутствующей оценки)."""
    if avg is None:
        return "⚪"
    avg = round(avg, 1)  # сравниваем с тем, что видно в тексте (например 70.0)
    if avg < 70:
        return "⚪"
    if avg < 80:
        return "🔴"
    if avg < 90:
        return "🟠"
    return "🟢"


def format_grades(data) -> str:
    """Собирает общий список предметов, сортирует по баллу и добавляет цвета."""
    subjects = find_subjects(data)
    if not subjects:
        return "Предметы в ответе не найдены (возможно, за этот семестр нет журнала)."

    rows = []
    for sub in subjects:
        avg = average_of(sub)
        if avg == 0:  # нулевые оценки не показываем
            continue
        rows.append((sub, avg))

    # От высокого к низкому; предметы без данных уходят вниз.
    rows.sort(key=lambda r: (r[1] is None, -(r[1] or 0)))

    if not rows:
        return "Нет предметов с оценками выше нуля."

    with_teachers = show_teachers()
    previous = load_last_grades()
    lines = []
    last_color = None
    for sub, avg in rows:
        subject_name = str(sub.get("subjectName", "?"))
        name = html.escape(subject_name)
        avg_text = f"{avg:.1f}" if avg is not None else "нет данных"
        old_avg = previous.get(subject_name)
        if avg is not None and old_avg is not None:
            delta = round(avg - float(old_avg), 1)
            if delta:
                avg_text += f" ({delta:+.1f})"
        emoji = mark_emoji(avg)
        if last_color is not None and emoji != last_color:
            lines.append("")
        last_color = emoji
        who = f" <i>· {html.escape(str(sub.get('tutorList', '?')))}</i>" if with_teachers else ""
        lines.append(f"{emoji}  <b>{name}</b>{who}  <code>{avg_text}</code>")

    return "<b>📊 СРЕДНИЙ БАЛЛ</b>\n" + "\n".join(lines)


def get_last_grades_rows(data) -> list[tuple[dict, float | None]]:
    """Возвращает отсортированные предметы и средние баллы."""
    rows = []
    for sub in find_subjects(data):
        avg = average_of(sub)
        if avg == 0:
            continue
        rows.append((sub, avg))
    rows.sort(key=lambda r: (r[1] is None, -(r[1] or 0)))
    return rows


def load_last_grades() -> dict[str, float | None]:
    """Загружает предыдущие средние баллы по названиям предметов."""
    try:
        with open("last_grades.json", encoding="utf-8") as f:
            previous_rows = json.load(f)
    except (OSError, ValueError):
        return {}
    try:
        return {row["subject"]: row.get("average") for row in previous_rows}
    except (TypeError, KeyError):
        return {}


def save_last_grades(data) -> None:
    """Сохраняет названия предметов и средние баллы в компактный JSON."""
    rows = get_last_grades_rows(data)

    with open("last_grades.json", "w", encoding="utf-8") as f:
        json.dump(
            [{"subject": sub.get("subjectName", "?"), "average": avg} for sub, avg in rows],
            f,
            ensure_ascii=False,
            separators=(",", ":"),
        )


# [SCHEDULE: CONFIG] Временная зона, JSON-файл и соответствие имени дня индексу.
TZ = ZoneInfo("Asia/Almaty")
SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedule.json")
DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница"]
DAY_INDEX = {n.lower(): i for i, n in enumerate(DAY_NAMES)}


def norm(s: str) -> str:
    """Нормализует названия для сравнения: нижний регистр, пробелы и ё/е."""
    return re.sub(r"\s+", " ", s.lower().replace("ё", "е")).strip()


def load_schedule():
    """Загружает JSON и готовит расписание, звонки и сокращения к поиску."""
    try:
        with open(SCHEDULE_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except OSError:
        log.warning("Файл %s не найден - расписание отключено", SCHEDULE_FILE)
        return {}, {}, {}
    except (json.JSONDecodeError, TypeError) as e:
        log.error("Некорректный JSON в %s: %s", SCHEDULE_FILE, e)
        return {}, {}, {}

    try:
        schedule = {
            DAY_INDEX[day.lower()]: [
                (int(pair["number"]), [(alt["subject"], alt.get("teacher", "")) for alt in pair["alternatives"]])
                for pair in pairs
            ]
            for day, pairs in data["days"].items()
        }
        bells = {int(number): value for number, value in data.get("bells", {}).items()}
        aliases = {norm(abbr): norm(full) for abbr, full in data.get("aliases", {}).items()}
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        log.error("Неверная структура расписания в %s: %s", SCHEDULE_FILE, e)
        return {}, {}, {}
    return schedule, bells, aliases


SCHEDULE, BELLS, ALIASES = load_schedule()
# [SCHEDULE: INIT] Расписание читается один раз при импорте; перезапуск нужен для его обновления.


def build_lookup(data) -> dict[str, float | None]:
    """Строит индекс нормализованных названий предметов и средних баллов."""
    return {norm(s.get("subjectName", "")): average_of(s) for s in find_subjects(data)}


def find_avg(name: str, lookup: dict[str, float | None]) -> tuple[bool, float | None]:
    """Ищет предмет из расписания среди предметов журнала.

    Возвращает (найден ли предмет, средний балл или None).
    """
    key = norm(name)
    key = ALIASES.get(key, key)
    if key in lookup:
        return True, lookup[key]
    for k, v in lookup.items():
        if k and (key in k or k in key):
            return True, v
    close = difflib.get_close_matches(key, list(lookup), n=1, cutoff=0.75)
    if close:
        return True, lookup[close[0]]
    return False, None


def format_day(day: int, lookup: dict[str, float | None], header: str | None = None) -> str:
    """Собирает расписание одного дня, добавляя время пары и найденную оценку."""
    pairs = SCHEDULE.get(day)
    if not pairs:
        return f"{header or DAY_NAMES[day] + ':'}\nПар нет."
    with_teachers = show_teachers()
    lines = [f"<b>📅 {html.escape(header or f'{DAY_NAMES[day]} · {len(pairs)} пар')}</b>"]
    for n, alts in pairs:
        parts = []
        for name, teacher in alts:
            text = html.escape(name)
            if with_teachers and teacher:
                text += f" <i>· {html.escape(teacher)}</i>"
            found, avg = find_avg(name, lookup)
            if avg is not None and avg != 0:
                text = f"{mark_emoji(avg)} {text}  <code>{avg:.1f}</code>"
            elif found:
                text = f"⚪ {text}  <i>оценок пока нет</i>"  # в журнале есть, но оценок пока нет (0 или пусто)
            else:
                text = f"❔ {text}  <i>нет в журнале</i>"  # название не совпало с журналом
            parts.append(text)
        time = f" ({BELLS[n]})" if n in BELLS else ""
        if len(parts) == 1:
            lines.append(f"<b>{n} пара{html.escape(time)}</b>  {parts[0]}")
        else:
            # несколько предметов в одной паре - каждый с новой строки
            lines.append(f"<b>{n} пара{html.escape(time)}</b>")
            lines.extend(f"　{part}" for part in parts)
    return "\n".join(lines)


def format_today(lookup: dict[str, float | None]) -> str:
    """Выбирает день недели по времени Алматы и форматирует сегодняшнее расписание."""
    wd = datetime.now(TZ).weekday()
    header = "Расписание на сегодня:"
    if wd > 4:
        return f"<b>📅 {header}</b>\nСегодня выходной, пар нет."
    return format_day(wd, lookup, header=header)


# [TELEGRAM: HANDLERS] Общий клиент Platonus и проверка владельца для команд.
platonus = Platonus(PLATONUS_LOGIN, PLATONUS_PASSWORD)


def allowed(update: Update) -> bool:
    """Разрешает команду только Telegram-пользователям из ALLOWED_USER_IDS."""
    return update.effective_user is not None and update.effective_user.id in ALLOWED_USER_IDS


async def grades_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/grades: проверяет доступ и аргументы, получает журнал, отвечает оценками/расписанием."""
    if not allowed(update):
        return
    year, term = DEFAULT_YEAR, DEFAULT_TERM
    weekday = None  # None = общий список + расписание на сегодня
    args = context.args
    if len(args) == 1 and args[0].isdigit() and 1 <= int(args[0]) <= 7:
        weekday = int(args[0]) - 1
    elif len(args) == 2:
        year, term = args
    elif args:
        await update.message.reply_text(
            "Использование:\n/grades - оценки и расписание на сегодня\n"
            "/grades 1..5 - расписание на день недели (1 - понедельник)\n"
            "/grades 2026 1 - оценки за другой семестр"
        )
        return
    grades_data = None
    try:
        data = await asyncio.to_thread(platonus.journal, year, term)
        lookup = build_lookup(data)
        if weekday is None:
            grades_data = data
            text = format_grades(data)
            text += "\n\n" + format_today(lookup)
        elif weekday > 4:
            text = "В выходной пар нет."
        else:
            text = format_day(weekday, lookup)
    except Exception as e:
        log.exception("Ошибка получения оценок")
        text = f"Не удалось получить оценки: {e}\nПопробуйте /relogin"
    # лимит Telegram - 4096 символов на сообщение
    for i in range(0, len(text), 4000):
            await update.message.reply_text(text[i:i + 4000], parse_mode="HTML")
    if grades_data is not None:
        await asyncio.to_thread(save_last_grades, grades_data)


async def relogin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/relogin: выполняет принудительный вход в Platonus и сообщает результат."""
    if not allowed(update):
        return
    try:
        await asyncio.to_thread(platonus.relogin)
        await update.message.reply_text("Сессия восстановлена.")
    except Exception as e:
        log.exception("Ошибка перелогина")
        await update.message.reply_text(f"Не удалось войти: {e}")


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/today: показывает отметки, дата которых совпадает с сегодняшней в Алматы."""
    if not allowed(update):
        return
    try:
        today = datetime.now(TZ).date()
        marks = await asyncio.to_thread(
            platonus.marks_for_date, DEFAULT_YEAR, DEFAULT_TERM, today
        )
        if not marks:
            text = f"<b>📅 Оценки за сегодня · {today:%d.%m.%Y}</b>\nЗа сегодня новых оценок нет."
        else:
            lines = [f"<b>📅 Оценки за сегодня · {today:%d.%m.%Y}</b>"]
            for item in marks:
                score = html.escape(str(item["mark"] if item["mark"] is not None else item["symbol"] or "—"))
                lines.append(f"{mark_emoji(float(item['mark']) if item['mark'] is not None else None)}  <b>{html.escape(str(item['subject']))}</b>  <code>{score}</code>")
            text = "\n".join(lines)
    except Exception as e:
        log.exception("Ошибка получения оценок за сегодня")
        text = f"Не удалось получить оценки за сегодня: {html.escape(str(e))}"
    await update.message.reply_text(text, parse_mode="HTML")


async def with_network_retries(action, description: str, attempts: int = 5):
    """Retries temporary Telegram/Platonus network failures with backoff."""
    retryable = (TimedOut, NetworkError, requests.exceptions.Timeout, requests.exceptions.ConnectionError)
    for attempt in range(1, attempts + 1):
        try:
            return await action()
        except retryable:
            if attempt == attempts:
                raise
            delay = min(2 ** attempt, 20)
            log.warning("Сетевая ошибка (%s), повтор %s/%s через %s сек.", description, attempt, attempts, delay)
            await asyncio.sleep(delay)


async def send_lesson_notification(bot_client, number: int, alternatives: list, target_date):
    """Builds and sends the same lesson update for scheduled and manual testing."""
    subject_names = [name for name, _teacher in alternatives]
    marks = await with_network_retries(
        lambda: asyncio.to_thread(
            platonus.marks_for_date,
            DEFAULT_YEAR,
            DEFAULT_TERM,
            target_date,
            subject_names,
        ),
        "получение оценок из Platonus",
    )
    lines = [f"<b>🔔 Пара {number} · {target_date:%d.%m.%Y}</b>"]
    for name in subject_names:
        normalized_name = ALIASES.get(norm(name), norm(name))
        matching = [m for m in marks if norm(m["subject"]) == norm(name)
                    or norm(m["subject"]) == normalized_name
                    or normalized_name in norm(m["subject"])
                    or norm(m["subject"]) in normalized_name]
        if matching:
            for item in matching:
                score = html.escape(str(item["mark"] if item["mark"] is not None else item["symbol"] or "—"))
                lines.append(f"{mark_emoji(float(item['mark']) if item['mark'] is not None else None)} <b>{html.escape(name)}</b>: <code>{score}</code>")
        else:
            lines.append(f"⚪ <b>{html.escape(name)}</b>: оценки за сегодня нет")
    for user_id in ALLOWED_USER_IDS:
        await with_network_retries(
            lambda user_id=user_id: bot_client.send_message(
                chat_id=user_id, text="\n".join(lines), parse_mode="HTML"
            ),
            "отправка уведомления в Telegram",
        )


async def schedule_notifications(application: Application) -> None:
    """Checks bell end times and sends one assessment update per finished lesson."""
    sent_today = set()
    pending = set()
    last_attempt = {}
    while True:
        now = datetime.now(TZ)
        today_key = now.date().isoformat()
        if now.weekday() <= 4:
            pairs = SCHEDULE.get(now.weekday(), [])
            for number, alternatives in pairs:
                bell = BELLS.get(number, "")
                match = re.search(r"-(\d{1,2}):(\d{2})$", bell)
                if not match:
                    continue
                key = (today_key, number)
                if (now.hour, now.minute) == (int(match.group(1)), int(match.group(2))):
                    pending.add(key)
                if key not in pending or key in sent_today:
                    continue
                if now.timestamp() - last_attempt.get(key, 0) < 60:
                    continue
                last_attempt[key] = now.timestamp()
                try:
                    await send_lesson_notification(application.bot, number, alternatives, now.date())
                    sent_today.add(key)
                    pending.discard(key)
                except Exception:
                    log.exception("Не удалось отправить уведомление по паре %s; повторю попытку через минуту", number)
        await asyncio.sleep(15)


async def post_init(application: Application) -> None:
    """Starts the schedule watcher after Telegram application initialization."""
    application.create_task(schedule_notifications(application), name="schedule-notifications")


async def test_notification(day: int | None = None, pair_number: int | None = None) -> None:
    """Sends a real one-off lesson notification from the console for testing."""
    today = datetime.now(TZ).date()
    weekday = today.weekday() if day is None else day - 1
    if weekday > 4:
        raise RuntimeError("На выбранный день нет занятий. Укажите --day от 1 (понедельник) до 5 (пятница).")
    pairs = SCHEDULE.get(weekday, [])
    pair = next((item for item in pairs if item[0] == pair_number), None) if pair_number else (pairs[0] if pairs else None)
    if pair is None:
        raise RuntimeError(f"Для выбранного дня не найдена пара {pair_number}.")
    bot_client = Bot(BOT_TOKEN, request=HTTPXRequest(
        connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=30,
    ))
    try:
        await with_network_retries(bot_client.initialize, "подключение к Telegram")
        await send_lesson_notification(bot_client, pair[0], pair[1], today)
    finally:
        await bot_client.shutdown()
    print(f"Тестовое уведомление отправлено: {DAY_NAMES[weekday]}, {pair[0]} пара.")


async def teachers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/teachers on|off - показывать ли ФИ преподавателей; без аргумента переключает."""
    if not allowed(update):
        return
    settings = load_settings()
    current = settings.get("show_teachers", True)
    arg = context.args[0].lower() if context.args else None
    if arg in ("on", "вкл", "1"):
        new = True
    elif arg in ("off", "выкл", "0"):
        new = False
    else:
        new = not current
    settings["show_teachers"] = new
    save_settings(settings)
    await update.message.reply_text(
        "ФИ преподавателей: " + ("показываю" if new else "скрыто")
    )


def main():
    """Строит Telegram-приложение, подключает команды и запускает polling."""
    if "--test-notification" in sys.argv:
        import argparse

        parser = argparse.ArgumentParser(description="Ручная проверка Telegram-уведомления об оценках")
        parser.add_argument("--test-notification", action="store_true", help="отправить уведомление себе")
        parser.add_argument("--day", type=int, choices=range(1, 6), help="день недели: 1=пн ... 5=пт (по умолчанию сегодня)")
        parser.add_argument("--pair", type=int, choices=range(1, 5), help="номер пары (по умолчанию первая пара дня)")
        args = parser.parse_args()
        asyncio.run(test_notification(args.day, args.pair))
        return
    try:
        telegram_request = HTTPXRequest(
            connect_timeout=30, read_timeout=30, write_timeout=30, pool_timeout=30,
        )
        app = (
            Application.builder()
            .token(BOT_TOKEN)
            .request(telegram_request)
            .get_updates_request(HTTPXRequest(
                connect_timeout=30, read_timeout=40, write_timeout=30, pool_timeout=30,
            ))
            .post_init(post_init)
            .build()
        )
    except Exception as e:
        raise RuntimeError(
            "Не удалось запустить Telegram-бота. Проверьте BOT_TOKEN и установку зависимостей."
        ) from e
    app.add_handler(CommandHandler("teachers", teachers_cmd))
    app.add_handler(CommandHandler("grades", grades_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("relogin", relogin_cmd))
    app.run_polling()


# [STARTUP: ENTRY POINT] Этот блок выполняется только при `py bot.py`.
if __name__ == "__main__":
    main()
