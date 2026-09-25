import json
import logging
import os
import random
import time
import urllib.request
from urllib.error import URLError

from fastapi import FastAPI, Request, Form, File, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import models

# Абсолютный путь к папке приложения — рядом с этим файлом, а не
# относительно текущей рабочей директории процесса. Та же причина,
# что и в models.py: если приложение когда-либо запущено не из
# /opt/englishapp (например, вручную для отладки из другой папки),
# относительные пути "static"/"templates" не найдутся вообще —
# сервер просто не запустится, а не молча сломается наполовину,
# но лучше не допускать самого повода для путаницы.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Настройка приложения ---

app = FastAPI()

# Подключаем папку static/ (CSS/JS) по адресу /static/...
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# Подключаем Jinja2 — шаблонизатор, который будет заполнять HTML данными из Python
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# --- Логирование ---
#
# Пишем через стандартный logging в stdout/stderr — процесс запущен под
# systemd (englishapp.service), а он сам собирает весь вывод процесса в
# journald. Поэтому НЕ заводим свои файлы логов и ротацию — всё уже
# смотрится через `journalctl -u englishapp` (живьём — `journalctl -u
# englishapp -f`). basicConfig без указания filename пишет в stderr —
# ровно то, что нужно.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("englishapp")


def log_event(event: str, **fields):
    """
    Одна строка лога вида "event key=val key2=val2 ..." — удобно и
    читать глазами в journalctl, и grep-ить/awk-ить потом для анализа.
    Что именно логировать для конкретного действия — решает вызывающий
    код на месте (см. вызовы log_event по всему файлу), сюда просто
    передаются готовые поля.
    """
    details = " ".join(f"{key}={value!r}" for key, value in fields.items())
    logger.info("%s %s", event, details)


def request_meta(request: Request) -> dict:
    """IP и user-agent запроса — общие поля почти для всех доменных событий."""
    return {
        "ip": request.client.host if request.client else "-",
        "ua": request.headers.get("user-agent", "-"),
    }


@app.middleware("http")
async def log_requests(request: Request, call_next):
    """
    Строка лога на КАЖДЫЙ HTTP-запрос: метод, путь, код ответа, время
    выполнения, IP, user-agent — независимо от того, что конкретно
    делает роут (общий access-log). Доменные события внутри роутов
    (какое слово добавили, что ответили в тренажёре и т.п.) логируются
    отдельно через log_event — они не дублируют друг друга, просто
    разного масштаба: этот middleware видит "что за HTTP-запрос",
    log_event внутри роута — "что конкретно произошло".
    """
    started = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - started) * 1000
    logger.info(
        "http_request method=%s path=%s status=%s duration_ms=%.1f ip=%s ua=%r",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        request.client.host if request.client else "-",
        request.headers.get("user-agent", "-"),
    )
    return response


# --- Автосброс кэша static/style.css в браузере ---
#
# Раньше при обновлении style.css на сервере браузер мог продолжать
# показывать старую версию из своего кэша, пока пользователь вручную
# не сделает жёсткое обновление (Ctrl+Shift+R) — визуально выглядело
# так, будто фикс "не применился". css_version — время последнего
# изменения файла (unix-время в секундах); он приклеивается к ссылке
# на стиль как ?v=... на ВСЕХ страницах (см. templates.env.globals
# ниже), и меняется сам собой при каждом обновлении style.css —
# браузер видит новый URL и качает файл заново, без спецдействий с
# нашей стороны. Считается один раз при старте процесса — это
# согласуется с тем, что для применения кода всё равно нужен restart
# сервиса, так что отдельно ничего запоминать не нужно.
def _style_version() -> str:
    try:
        return str(int(os.path.getmtime(os.path.join(BASE_DIR, "static", "style.css"))))
    except OSError:
        return "0"


templates.env.globals["css_version"] = _style_version()

# Словари (категории) теперь хранятся в базе (таблица categories) и создаются
# из интерфейса — см. models.get_categories(). Раньше здесь был зашитый список.

# Уровни сложности слов — используются в формах добавления/редактирования
DIFFICULTIES = ["easy", "medium", "hard"]

# Сколько слов показывать в "сложнее всего" на /stats — раньше был
# зашитый в модели лимит 10, из-за которого новые слова с ошибками
# не всегда попадали в список, если в категории уже набралось 10+
# слов с ошибками похуже.
HARDEST_WORDS_LIMIT = 30

# Верхние границы длины текстовых полей слова — защита от случайного
# (или намеренного) вставленного огромного блока текста в поле формы:
# без лимита такая запись раздувает базу и ломает вёрстку таблицы в
# админке (одна строка с абзацем текста растягивает всю таблицу).
MAX_QUESTION_LEN = 200
MAX_ANSWER_LEN = 500
MAX_EXTRA_LEN = 300


def clean_category(value: str) -> str:
    """
    Проверяет category против списка существующих словарей. HTML-форма
    и так предлагает только валидные варианты через <select>, но это
    не мешает прямому POST-запросу (curl, испорченная форма, чужой
    скрипт) прислать что угодно — сервер не должен слепо доверять
    вводу браузера. Неизвестное значение тихо заменяется на первый
    словарь из списка, а не роняет запрос ошибкой 500: потерянное слово
    хуже, чем слово в чуть неверной категории, которое легко поправить
    потом в админке.
    """
    categories = models.get_categories()
    if value in categories:
        return value
    return categories[0] if categories else value


def clean_difficulty(value: str) -> str:
    """Проверяет difficulty против known-списка, иначе — medium по умолчанию."""
    return value if value in DIFFICULTIES else "medium"


def clean_text(value: str, max_len: int) -> str:
    """Обрезает текстовое поле до разумной длины и убирает пробелы по краям."""
    return value.strip()[:max_len]


def parse_levels(raw: str) -> list[str]:
    """
    Разбирает фильтр сложности из URL: строка вида "easy,medium" →
    ["easy", "medium"] (в порядке DIFFICULTIES, без мусора и повторов).
    Пустая строка и "выбраны все три уровня" одинаково означают "без
    фильтра" — возвращается пустой список.
    """
    wanted = {part.strip() for part in (raw or "").split(",")}
    levels = [d for d in DIFFICULTIES if d in wanted]
    return [] if len(levels) == len(DIFFICULTIES) else levels


def difficulty_filter_links(levels: list[str]) -> list[dict]:
    """
    Для каждого уровня считает значение параметра difficulty, которое
    получится после клика по нему: если уровень уже выбран — он
    убирается из набора, иначе добавляется. Так можно включить любую
    комбинацию (например, easy + medium). Когда фильтр пуст ("все"),
    клик по уровню начинает выбор с него одного.
    """
    links = []
    for d in DIFFICULTIES:
        chosen = set(levels)
        chosen.symmetric_difference_update({d})
        value = ",".join(parse_levels(",".join(chosen)))
        links.append({"name": d, "active": d in levels, "value": value})
    return links


def practice_url(category: str, reverse, difficulty: str) -> str:
    """Собирает безопасный URL /practice — параметры прогоняются через валидацию."""
    levels = ",".join(parse_levels(difficulty))
    return f"/practice?category={clean_category(category)}&reverse={1 if int(reverse) else 0}&difficulty={levels}"


# Во сколько раз чаще выпадает слово из избранного
FAVORITE_WEIGHT = 3


def _word_weight(word) -> float:
    """
    Вес одного слова для взвешенного случайного выбора — см. докстринг
    pick_random_word. times_shown/times_wrong берутся из
    models.get_words_for_practice (LEFT JOIN со статистикой попыток):
    - coverage: ни разу не показанное слово получает вес 1.0, дальше
      падает с каждым показом (показано 1 раз -> 0.5, 4 раза -> 0.2...)
      — редко встречавшиеся слова становятся заметно вероятнее.
    - error_bonus: каждая записанная ошибка добавляет +1 к весу —
      слово, в котором путаются, продолжает чаще попадаться на
      повторение, даже когда оно уже давно не новое.
    Хорошо изученное слово (много показов, мало ошибок) не исчезает
    совсем — вес просто маленький, а не нулевой, так что изредка оно
    всё равно повторится.
    """
    coverage = 1.0 / (1 + (word["times_shown"] if "times_shown" in word.keys() else 0))
    error_bonus = 1 + (word["times_wrong"] if "times_wrong" in word.keys() else 0)
    favorite = FAVORITE_WEIGHT if word["favorite"] else 1
    return coverage * error_bonus * favorite


def pick_random_word(words):
    """
    Выбирает следующее слово для тренажёра в два шага:

    1. Если среди переданных слов больше одного уровня сложности
       (фильтр по сложности не выбран пользователем) — сначала с
       РАВНОЙ вероятностью выбирается один уровень из тех, что реально
       присутствуют в выборке, и только потом слово внутри него. Без
       этого шага уровень с большим числом слов забивал бы собой
       остальные просто количеством — например, при 663 словах уровня
       easy и 88 словах уровня hard слово из hard попадалось бы почти
       в 8 раз реже среднего easy-слова просто по объёму словаря, а не
       потому что оно "легче". С этим шагом у easy и у hard в целом
       одинаковые шансы быть показанным следующим, независимо от того,
       сколько слов в каждом уровне.
    2. Внутри выбранного уровня (или сразу, если фильтр по сложности
       уже применён и уровень один) — взвешенный случайный выбор по
       _word_weight: слова, показанные реже или вообще ни разу, и
       слова, в которых чаще ошибались, получают больше шансов.
    3. Избранное (favorite) добавляет множитель FAVORITE_WEIGHT поверх
       всего вышеперечисленного — считается внутри _word_weight.
    """
    tiers: dict[str, list] = {}
    for w in words:
        tiers.setdefault(w["difficulty"], []).append(w)
    pool = random.choice(list(tiers.values()))
    weights = [_word_weight(w) for w in pool]
    return random.choices(pool, weights=weights, k=1)[0]


def google_translate_url(word: str) -> str:
    """Ссылка на Google Translate с уже подставленным словом (en → ru)."""
    from urllib.parse import quote
    return f"https://translate.google.com/?sl=en&tl=ru&text={quote(word)}&op=translate"


def reverso_context_url(word: str) -> str:
    """Ссылка на Reverso Context с уже подставленным словом (контекстный перевод en → ru)."""
    from urllib.parse import quote
    return f"https://context.reverso.net/translation/english-russian/{quote(word)}"


@app.on_event("startup")
def startup():
    """
    Выполняется один раз при запуске сервера (uvicorn main:app).
    Создаёт таблицу words в базе, если её ещё нет.
    """
    models.init_db()


# --- Главная страница: просто ссылки на практику и админку ---

FACT_API_URL = "https://uselessfacts.jsph.pl/api/v2/facts/random?language=en"
FACT_API_TIMEOUT = 3  # секунды — не задерживаем ответ пользователю, если внешний сайт тормозит


def fetch_external_fact() -> str | None:
    """
    Тянет случайный факт с внешнего сайта (uselessfacts.jsph.pl,
    бесплатный, без ключа) — факты настоящие и на английском, что для
    тренажёра английского даже уместнее, чем выдуманный локальный
    список. При любой проблеме (сеть недоступна, таймаут, сайт лёг,
    неожиданный формат ответа) тихо возвращает None — вызывающий код
    в этом случае берёт факт из локального списка (models.random_fact),
    чтобы баннер никогда не оставался пустым из-за стороннего сайта.
    """
    try:
        with urllib.request.urlopen(FACT_API_URL, timeout=FACT_API_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        text = data.get("text", "").strip()
        return text or None
    except (URLError, TimeoutError, json.JSONDecodeError, AttributeError, ValueError):
        return None


def get_fact() -> str:
    """Факт для баннера: сначала пробуем внешний сайт, при сбое — локальный список."""
    return fetch_external_fact() or models.random_fact()


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        request,
        "home.html",
        {"categories": models.get_categories(), "fact": get_fact()},
    )


@app.get("/api/fact")
def api_fact():
    """
    Новый случайный факт для баннера на главной — вызывается кнопкой
    "обновить" через fetch, без перезагрузки страницы.
    """
    return {"fact": get_fact()}


# --- Тренажёр (практика) ---

@app.get("/practice")
def practice(
    request: Request,
    category: str = "english",
    reverse: int = 0,
    difficulty: str = "",
    check_id: int | None = None,
    check_answer: str = "",
    just_answered: int = 0,
    shown: str = "",
    shown_q: str = "",
):
    """
    Показывает одно случайное слово/задание из выбранной категории —
    один и тот же режим проверки для ВСЕХ категорий:

    - reverse=0 (по умолчанию): вопрос — question, ответ — answer
    - reverse=1: вопрос — один из переводов (выбирается случайно из
      всех вариантов answer), ответ — question

    И question, и answer могут содержать НЕСКОЛЬКО принимаемых
    вариантов через ";" или "," — например, answer "seller, salesman"
    для question "продавец" (или question "metro; tube; underground"
    для answer "метро"). Засчитывается любой вариант с любой стороны,
    без учёта регистра, пробелов по краям и различия е/ё. Это те же
    "синонимы", что и обычные альтернативные переводы — просто с
    другой стороны карточки.

    difficulty — необязательный фильтр: один уровень или несколько через
    запятую ("easy,medium"); пусто — все уровни.

    Слова из избранного выпадают чаще (см. FAVORITE_WEIGHT).

    shown — какой из вариантов answer был показан как вопрос в
    reverse-режиме. shown_q — какой из вариантов question был показан
    как вопрос в обычном режиме. Оба передаются формой проверки
    обратно, чтобы после ответа на экране осталась та же формулировка,
    а не новая случайная.

    Каждая проверка записывается в статистику через
    models.record_attempt со статусом 'correct'/'incorrect'.
    Переход на следующее слово без проверки — 'skipped' (см. /practice/skip).

    just_answered=1 передаётся после верного ответа как сигнал для
    шаблона показать автопереход с возможностью отмены — сам по себе
    ни на что не влияет на сервере.
    """
    category = clean_category(category)
    reverse = 1 if reverse else 0
    levels = parse_levels(difficulty)
    difficulty = ",".join(levels)

    words = models.get_words_for_practice(category, levels or None)
    result = None  # None = ещё не проверяли; True/False = результат проверки

    base_context = {
        "categories": models.get_categories(),
        "current_category": category,
        "difficulties": DIFFICULTIES,
        "difficulty_links": difficulty_filter_links(levels),
        "current_difficulty": difficulty,
        "levels_label": ", ".join(levels),
        "reverse": reverse,
        "checked_answer": check_answer,
    }

    if not words:
        return templates.TemplateResponse(
            request,
            "practice.html",
            {
                **base_context,
                "word": None,
                "word_answer_primary": None,
                "word_question_primary": None,
                "shown_variant": None,
                "shown_question": None,
                "all_answers": [],
                "all_questions": [],
                "hint": None,
                "result": None,
                "translate_url": None,
                "reverso_url": None,
                "just_answered": False,
                "is_favorite": False,
            },
        )

    # Если check_id пришёл — значит форма проверки была отправлена,
    # ищем именно то слово, которое проверялось (а не новое случайное).
    # Если такого слова уже нет (удалено или не подходит под фильтр) —
    # просто берём новое.
    word = None
    if check_id is not None:
        word = next((w for w in words if w["id"] == check_id), None)
        if word is not None:
            correct_value = word["question"] if reverse else word["answer"]
            result = models.is_correct_answer(check_answer, correct_value)
            status = "correct" if result else "incorrect"
            shown_text = shown if reverse else shown_q
            models.record_attempt(word["id"], status, bool(reverse), shown_text=shown_text, typed_answer=check_answer)
            log_event(
                "practice_check",
                **request_meta(request),
                category=category,
                difficulty=difficulty or "all",
                reverse=bool(reverse),
                word_id=word["id"],
                shown=shown_text,
                typed=check_answer,
                result=status,
            )
    if word is None:
        word = pick_random_word(words)

    # И answer, и question могут содержать несколько вариантов через
    # ";"/"," (синонимы с обеих сторон — см. докстринг выше). all_* —
    # полный список принимаемых вариантов, primary — первый из них.
    all_answers = models.split_answers(word["answer"])
    word_answer_primary = models.primary_answer(word["answer"])
    all_questions = models.split_answers(word["question"])
    word_question_primary = models.primary_answer(word["question"])

    # Что показывать как вопрос: при reverse — случайный из переводов
    # (answer), иначе — случайный из вариантов самого слова (question).
    # При проверке (check_id) берём тот, что реально был на экране
    # (shown/shown_q), если он всё ещё входит в список вариантов —
    # иначе после ответа текст на экране "прыгнул" бы на другой синоним.
    if reverse and all_answers:
        if check_id is not None and shown in all_answers:
            shown_variant = shown
        else:
            shown_variant = random.choice(all_answers)
    else:
        shown_variant = word_answer_primary

    if not reverse and all_questions:
        if check_id is not None and shown_q in all_questions:
            shown_question = shown_q
        else:
            shown_question = random.choice(all_questions)
    else:
        shown_question = word_question_primary

    # Подсказка — по тому полю, которое сейчас является "ответом":
    # answer при обычном направлении, question при reverse. Берём
    # основной (первый) вариант — подсказка нацелена на одно
    # конкретное слово, а не на весь список синонимов сразу.
    hint_source = word_question_primary if reverse else word_answer_primary
    hint = models.hint_for(hint_source)

    # Ссылки на внешние переводчики — только для english, на основной
    # вариант самого английского слова, вне зависимости от reverse.
    translate_url = reverso_url = None
    if category == "english":
        translate_url = google_translate_url(word_question_primary)
        reverso_url = reverso_context_url(word_question_primary)

    return templates.TemplateResponse(
        request,
        "practice.html",
        {
            **base_context,
            "word": word,
            "word_answer_primary": word_answer_primary,
            "word_question_primary": word_question_primary,
            "shown_variant": shown_variant,
            "shown_question": shown_question,
            "all_answers": all_answers,
            "all_questions": all_questions,
            "hint": hint,
            "result": result,
            "translate_url": translate_url,
            "reverso_url": reverso_url,
            "just_answered": bool(just_answered) and result is True,
            "is_favorite": bool(word["favorite"]),
        },
    )


@app.post("/practice/skip")
def practice_skip(
    request: Request,
    word_id: int = Form(...),
    reverse: int = Form(...),
    category: str = Form(...),
    difficulty: str = Form(""),
    record: int = Form(1),
):
    """
    Переход к следующему слову. По умолчанию (record=1) записывает
    пропуск в статистику — вызывается кнопкой "Следующее слово",
    когда пользователь уходит, не проверив ответ.
    record=0 используется после ВЕРНОГО ответа (автопереход и кнопка
    "Следующее слово", в которую он превращается после отмены) —
    там попытка уже записана как 'correct' при самой проверке,
    второй раз (как skip) писать не нужно, иначе одна и та же
    попытка задвоится в статистике.
    """
    if record:
        models.record_attempt(word_id, "skipped", bool(reverse))
        log_event(
            "practice_skip",
            **request_meta(request),
            category=category,
            difficulty=difficulty or "all",
            reverse=bool(reverse),
            word_id=word_id,
        )
    return RedirectResponse(url=practice_url(category, reverse, difficulty), status_code=303)


@app.post("/practice/accept")
def practice_accept(
    request: Request,
    word_id: int = Form(...),
    category: str = Form(...),
    reverse: int = Form(...),
    difficulty: str = Form(""),
    user_answer: str = Form(...),
):
    """
    Добавляет введённый пользователем ответ как альтернативный
    вариант к слову — вызывается кнопкой "Засчитать как верный"
    после неверного ответа в тренажёре, для любой категории.
    Направление решает, в какое поле добавляется вариант: reverse=0
    (question → answer) дописывает перевод в answer; reverse=1
    (answer → question) дописывает синоним в question — ровно случай
    "seller, но и salesman тоже" или "metro/tube/underground".
    """
    field = "question" if reverse else "answer"
    if reverse:
        models.add_alternative_question(word_id, user_answer)
    else:
        models.add_alternative_answer(word_id, user_answer)
    log_event(
        "practice_accept",
        **request_meta(request),
        category=category,
        word_id=word_id,
        field=field,
        added=user_answer,
    )
    return RedirectResponse(url=practice_url(category, reverse, difficulty), status_code=303)


@app.post("/practice/favorite")
def practice_favorite(request: Request, word_id: int = Form(...)):
    """
    Переключает "избранное" у слова. Вызывается из тренажёра через
    fetch, без перезагрузки страницы — иначе выпало бы другое
    случайное слово. Возвращает новое состояние.
    """
    state = models.toggle_favorite(word_id)
    if state is None:
        return JSONResponse({"ok": False}, status_code=404)
    log_event("practice_favorite", **request_meta(request), word_id=word_id, favorite=state)
    return {"ok": True, "favorite": state}


# --- Статистика ---

@app.get("/stats")
def stats(request: Request):
    """
    Показывает сводную статистику по каждой категории (всего попыток,
    точность, отдельно число пропусков) и топ самых сложных слов —
    то, что чаще всего отвечали неверно. Категории без единой
    попытки не отображаются.

    В "сложнее всего" попадают только слова хотя бы с одной ПРОВЕРЕННОЙ
    попыткой (см. докстринг get_hardest_words) — новое слово, которое
    ни разу не показывали или ни разу не ответили неправильно, туда
    закономерно не попадёт, это не баг. limit увеличен с прежних 10 до
    HARDEST_WORDS_LIMIT, чтобы список не обрезался раньше времени.
    """
    by_category = models.get_stats_by_category()
    hardest = {
        s["category"]: models.get_hardest_words(s["category"], limit=HARDEST_WORDS_LIMIT)
        for s in by_category
    }
    return templates.TemplateResponse(
        request,
        "stats.html",
        {"by_category": by_category, "hardest": hardest, "hardest_limit": HARDEST_WORDS_LIMIT},
    )


# --- Админка: меню категорий + отдельная страница редактирования на каждую ---

@app.get("/admin")
def admin_list(
    request: Request,
    imported: int | None = None,
    skipped: int | None = None,
    purged: int | None = None,
    cat_status: str = "",
    cat_name: str = "",
):
    """
    Страница-меню: счётчик слов по каждому словарю (ссылка на
    /admin/<category> для редактирования) плюс общие для всего
    словаря функции — создание нового словаря, добавление слова
    (всплывающее окно), загрузка файлом, экспорт, чистка заглушек.

    cat_status/cat_name — результат создания/удаления словаря
    (created/exists/invalid/deleted/not_empty), только для сообщения.
    """
    categories = models.get_categories()
    counts = {cat: 0 for cat in categories}
    for w in models.get_all_words():
        if w["category"] in counts:
            counts[w["category"]] += 1

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "categories": categories,
            "counts": counts,
            "difficulties": DIFFICULTIES,
            "imported": imported,
            "skipped": skipped,
            "purged": purged,
            "cat_status": cat_status,
            "cat_name": cat_name,
            "current_category": None,
        },
    )


@app.post("/admin/categories/add")
def admin_category_add(request: Request, name: str = Form(...)):
    """Создаёт новый словарь. Имя нормализуется (см. models.normalize_category_name)."""
    from urllib.parse import quote

    created_name, status = models.add_category(name)
    log_event("category_add", **request_meta(request), name=created_name or name, status=status)
    return RedirectResponse(
        url=f"/admin?cat_status={status}&cat_name={quote(created_name or name.strip()[:40])}",
        status_code=303,
    )


@app.post("/admin/categories/delete")
def admin_category_delete(request: Request, name: str = Form(...)):
    """Удаляет словарь, только если в нём нет слов."""
    from urllib.parse import quote

    deleted = models.delete_category(name)
    status = "deleted" if deleted else "not_empty"
    log_event("category_delete", **request_meta(request), name=name, status=status)
    return RedirectResponse(url=f"/admin?cat_status={status}&cat_name={quote(name[:40])}", status_code=303)


@app.post("/admin/import")
def admin_import(request: Request, category: str = Form(...), file: UploadFile = File(...)):
    """
    Принимает файл со словами и добавляет все новые пары в базу,
    пропуская дубликаты (проверка по вопросу внутри категории,
    без учёта регистра) и заглушки-плейсхолдеры вида "перевод" без
    реального значения. Формат определяется по расширению файла:

    - .txt — одна запись на строку, разделитель между вопросом и
      ответом: -, —, : или таб (см. models.parse_word_list).
    - .json — список объектов {"word": ..., "translation": ...,
      "transcription": ..., "difficulty": ...} (см. models.parse_json_words).
      Транскрипция, если есть, попадает в поле extra; уровень
      сложности — в difficulty (по умолчанию medium).
    """
    category = clean_category(category)
    raw_bytes = file.file.read()
    text = raw_bytes.decode("utf-8", errors="ignore")
    filename = (file.filename or "").lower()

    if filename.endswith(".json"):
        quads = models.parse_json_words(text)
        report = models.bulk_add_words_with_extra(category, quads)
    else:
        pairs = models.parse_word_list(text)
        report = models.bulk_add_words(category, pairs)

    log_event(
        "import",
        **request_meta(request),
        category=category,
        filename=filename,
        added=report["added"],
        skipped=report["skipped"],
    )
    return RedirectResponse(
        url=f"/admin?imported={report['added']}&skipped={report['skipped']}",
        status_code=303,
    )


@app.get("/admin/export")
def admin_export(category: str | None = None):
    """
    Отдаёт словарь как скачиваемый JSON-файл — тот же формат полей,
    что принимает /admin/import, так что выгрузку можно сразу
    заливать обратно (резервная копия, перенос на другой сервер).
    Без параметра category выгружает всё сразу, с ним — только
    одну категорию.
    """
    from fastapi.responses import Response

    content = models.export_words_json(category)
    filename = f"words_{category}.json" if category else "words_all.json"
    return Response(
        content=content,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/purge-placeholders")
def admin_purge_placeholders(request: Request):
    """
    Удаляет из всех категорий слова, у которых answer — явная
    заглушка-плейсхолдер (буквально "перевод", "translation" и т.п.
    без реального перевода). Разовая чистка данных, накопившихся до
    того, как импорт начал фильтровать такое сам.
    """
    deleted = models.purge_placeholder_answers()
    log_event("purge_placeholders", **request_meta(request), deleted=deleted)
    return RedirectResponse(url=f"/admin?purged={deleted}", status_code=303)


@app.post("/admin/add")
def admin_add(
    request: Request,
    category: str = Form(...),
    question: str = Form(...),
    answer: str = Form(...),
    extra: str = Form(""),
    difficulty: str = Form("medium"),
):
    """
    Обрабатывает отправку формы добавления слова (всплывающее окно в
    админке). Окно отправляет форму через fetch с заголовком
    Accept: application/json — тогда возвращается JSON и страница
    остаётся на месте (окно просто закрывается, никаких переходов):

        {"ok": true,  "status": "added",     "word": {...}, "count": N}
        {"ok": false, "status": "duplicate"}   — такой вопрос уже есть
        {"ok": false, "status": "invalid"}     — пустой вопрос/ответ

    Без этого заголовка (обычная отправка формы) работает как раньше —
    редирект на страницу словаря.
    category/difficulty валидируются против известных списков (см.
    clean_category/clean_difficulty) — прямой POST в обход формы не
    должен пройти с произвольным мусором. question/answer/extra
    обрезаются до разумной длины по той же причине.
    Дубликат (такой вопрос уже есть в словаре) не добавляется — та же
    проверка, что и при импорте файла.
    """
    wants_json = "application/json" in request.headers.get("accept", "")

    category = clean_category(category)
    difficulty = clean_difficulty(difficulty)
    question = clean_text(question, MAX_QUESTION_LEN)
    answer = clean_text(answer, MAX_ANSWER_LEN)
    extra = clean_text(extra, MAX_EXTRA_LEN)

    status = "added"
    new_id = None
    if not question or not answer:
        status = "invalid"
    elif question.lower() in models.get_existing_questions(category):
        status = "duplicate"
    else:
        new_id = models.add_word(category, question, answer, extra, difficulty)

    log_event(
        "word_add",
        **request_meta(request),
        category=category,
        status=status,
        word_id=new_id,
        question=question,
    )

    if not wants_json:
        return RedirectResponse(url=f"/admin/{category}", status_code=303)

    payload = {"ok": status == "added", "status": status, "category": category}
    if new_id is not None:
        payload["count"] = models.count_words_by_category(category)
        payload["word"] = {
            "id": new_id,
            "category": category,
            "question": question,
            "answer": answer,
            "extra": extra,
            "difficulty": difficulty,
        }
    return JSONResponse(payload)


@app.post("/admin/edit/{word_id}")
def admin_edit(
    request: Request,
    word_id: int,
    category: str = Form(...),
    question: str = Form(...),
    answer: str = Form(...),
    extra: str = Form(""),
    difficulty: str = Form("medium"),
    per_page: str = Form("50"),
):
    """
    Обрабатывает отправку формы редактирования конкретного слова.
    Та же валидация и обрезка полей, что и в admin_add — см. там.
    per_page передаётся формой (скрытое поле) и возвращается в
    редиректе — без этого страница после сохранения всегда
    открывалась бы с дефолтным размером страницы (50), даже если
    до этого было выбрано "показать все" или "100".
    После сохранения — редирект с highlight=<id>: страница сама
    прокручивается к этой строке и ставит её по центру экрана (JS в
    admin_category.html). Якорь #word-<id> в адресе не используется —
    браузер по нему прижимал строку к верху, под липкую шапку.
    """
    category = clean_category(category)
    difficulty = clean_difficulty(difficulty)
    question = clean_text(question, MAX_QUESTION_LEN)
    answer = clean_text(answer, MAX_ANSWER_LEN)
    extra = clean_text(extra, MAX_EXTRA_LEN)

    if not question or not answer:
        return RedirectResponse(url=f"/admin/{category}?per_page={per_page}", status_code=303)

    models.update_word(word_id, category, question, answer, extra, difficulty)
    log_event("word_edit", **request_meta(request), word_id=word_id, category=category, question=question)
    return RedirectResponse(
        url=f"/admin/{category}?highlight={word_id}&per_page={per_page}",
        status_code=303,
    )


@app.post("/admin/delete/{word_id}")
def admin_delete(request: Request, word_id: int, category: str = Form(...), per_page: str = Form("50")):
    """
    Удаляет слово и возвращает на страницу категории, к позиции, где
    оно было — подсвечивает и скроллит к соседнему слову (следующему
    по порядку, а если удалённое было последним — к предыдущему),
    вместо того чтобы просто открывать страницу заново с самого верха.
    Сосед определяется ДО удаления, иначе искать уже нечего.
    per_page сохраняется в редиректе — та же причина, что в admin_edit.
    """
    neighbor_id = models.get_neighbor_word_id(word_id, category)
    models.delete_word(word_id)
    log_event("word_delete", **request_meta(request), word_id=word_id, category=category)
    if neighbor_id:
        return RedirectResponse(
            url=f"/admin/{category}?highlight={neighbor_id}&per_page={per_page}",
            status_code=303,
        )
    return RedirectResponse(url=f"/admin/{category}?per_page={per_page}", status_code=303)


@app.get("/admin/{category}")
def admin_category(
    request: Request,
    category: str,
    highlight: int | None = None,
    page: int = 1,
    per_page: str = "50",
):
    """
    Страница редактирования одной категории — таблица её слов.
    Неизвестная категория (не из списка словарей) просто покажет пустой
    список, а не ошибку — работает как "ничего не нашлось", не падает.

    Объявлен ПОСЛЕДНИМ среди /admin/* роутов намеренно: это путь с
    параметром {category}, и FastAPI матчит роуты по порядку — если
    бы он стоял раньше /admin/export или /admin/import, он бы
    перехватывал их на себя (category="export" и т.п.) и они бы
    никогда не сработали.

    highlight — id слова, которое нужно подсветить и проскроллить к
    нему (передаётся после успешного редактирования) — используется
    только фронтендом (CSS :target плюс JS автоскролл в шаблоне),
    сам параметр ни на что на сервере не влияет.
    """
    # per_page="all" — особый случай: показать всё без разбивки (нужно,
    # когда словарь ещё небольшой, и постраничность только мешает).
    # Любое другое значение приводится к числу из разрешённого списка;
    # что угодно не из списка (опечатка в URL, чужой линк) откатывается
    # на дефолт — так же, как валидация category/difficulty в других
    # роутах: не должно ронять страницу ошибкой на кривом query-параметре.
    allowed_per_page = ["50", "100", "all"]
    if per_page not in allowed_per_page:
        per_page = "50"

    total = models.count_words_by_category(category)

    if per_page == "all":
        page = 1
        total_pages = 1
        words = models.get_words_by_category(category)
    else:
        page_size = int(per_page)
        total_pages = max(1, (total + page_size - 1) // page_size)

        # Если подсвечиваемое слово не попадает на запрошенную страницу
        # (типичный случай: отредактировал слово в конце длинного списка,
        # а открылась страница 1) — вычисляем страницу, где оно реально
        # лежит, и показываем её вместо той, что пришла в URL.
        if highlight is not None:
            all_ids = [w["id"] for w in models.get_words_by_category(category)]
            if highlight in all_ids:
                page = all_ids.index(highlight) // page_size + 1
            else:
                page = max(1, min(page, total_pages))
        else:
            page = max(1, min(page, total_pages))

        words = models.get_words_by_category(
            category, limit=page_size, offset=(page - 1) * page_size
        )

    return templates.TemplateResponse(
        request,
        "admin_category.html",
        {
            "category": category,
            "categories": models.get_categories(),
            "current_category": category,
            "words": words,
            "difficulties": DIFFICULTIES,
            "highlight": highlight,
            "page": page,
            "total_pages": total_pages,
            "per_page": per_page,
            "total": total,
        },
    )
