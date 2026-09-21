import os
import random

from fastapi import FastAPI, Request, Form, File, UploadFile
from fastapi.responses import RedirectResponse
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

# Список категорий — используется и в /practice, и в /admin (форма добавления)
CATEGORIES = ["english", "python", "linux", "sql"]

# Уровни сложности слов — используются в формах добавления/редактирования
DIFFICULTIES = ["easy", "medium", "hard"]

# Верхние границы длины текстовых полей слова — защита от случайного
# (или намеренного) вставленного огромного блока текста в поле формы:
# без лимита такая запись раздувает базу и ломает вёрстку таблицы в
# админке (одна строка с абзацем текста растягивает всю таблицу).
MAX_QUESTION_LEN = 200
MAX_ANSWER_LEN = 500
MAX_EXTRA_LEN = 300


def clean_category(value: str) -> str:
    """
    Проверяет category против списка известных категорий. HTML-форма
    и так предлагает только валидные варианты через <select>, но это
    не мешает прямому POST-запросу (curl, испорченная форма, чужой
    скрипт) прислать что угодно — сервер не должен слепо доверять
    вводу браузера. Неизвестное значение тихо заменяется на первую
    категорию из списка, а не роняет запрос ошибкой 500: для формы
    добавления слова это не критичная операция, которую стоит
    прерывать, потерянное слово хуже, чем слово в чуть неверной
    категории, которое легко поправить потом в админке.
    """
    return value if value in CATEGORIES else CATEGORIES[0]


def clean_difficulty(value: str) -> str:
    """Проверяет difficulty против known-списка, иначе — medium по умолчанию."""
    return value if value in DIFFICULTIES else "medium"


def clean_text(value: str, max_len: int) -> str:
    """Обрезает текстовое поле до разумной длины и убирает пробелы по краям."""
    return value.strip()[:max_len]


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

@app.get("/")
def home(request: Request):
    return templates.TemplateResponse(
        request, "home.html", {"categories": CATEGORIES}
    )


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
):
    """
    Показывает одно случайное слово/задание из выбранной категории —
    один и тот же режим проверки для ВСЕХ категорий (english/python/
    linux/sql):

    - reverse=0 (по умолчанию): вопрос — question, ответ — answer
    - reverse=1: вопрос — answer, ответ — question
    Слово может иметь несколько принимаемых ответов (через ";" или ","
    в answer) — засчитывается любой из них, без учёта регистра,
    пробелов по краям и различия е/ё.

    difficulty — необязательный фильтр (easy/medium/hard); пустая
    строка означает "все уровни".

    Каждая проверка записывается в статистику через
    models.record_attempt со статусом 'correct'/'incorrect'.
    Переход на следующее слово без проверки — 'skipped' (см. /practice/skip).

    just_answered=1 передаётся после верного ответа как сигнал для
    шаблона показать автопереход с возможностью отмены — сам по себе
    ни на что не влияет на сервере.
    """
    category = clean_category(category)
    reverse = 1 if reverse else 0
    difficulty = difficulty if difficulty in DIFFICULTIES else ""

    words = models.get_words_by_category(category, difficulty or None)
    result = None  # None = ещё не проверяли; True/False = результат проверки
    all_answers = []

    if not words:
        return templates.TemplateResponse(
            request,
            "practice.html",
            {
                "categories": CATEGORIES,
                "current_category": category,
                "difficulties": DIFFICULTIES,
                "current_difficulty": difficulty,
                "word": None,
                "word_answer_primary": None,
                "all_answers": all_answers,
                "hint": None,
                "reverse": reverse,
                "result": result,
                "checked_answer": check_answer,
                "translate_url": None,
                "reverso_url": None,
                "just_answered": False,
            },
        )

    # Если check_id пришёл — значит форма проверки была отправлена,
    # ищем именно то слово, которое проверялось (а не новое случайное)
    if check_id is not None:
        word = next((w for w in words if w["id"] == check_id), None)
        if word is not None:
            correct_value = word["question"] if reverse else word["answer"]
            result = models.is_correct_answer(check_answer, correct_value)
            models.record_attempt(word["id"], "correct" if result else "incorrect", bool(reverse))
    else:
        word = random.choice(words)

    # word.answer может содержать несколько вариантов через ";"/"," —
    # primary для показа "вопроса" при реверсе, all_answers — полный
    # список для отображения всех принимаемых переводов сразу.
    # sqlite3.Row нельзя дополнить новым полем на лету, поэтому
    # считаем отдельно и передаём в шаблон своими переменными.
    word_answer_primary = models.primary_answer(word["answer"]) if word else None
    all_answers = models.split_answers(word["answer"]) if word else []

    # Подсказка — по тому полю, которое сейчас является "ответом":
    # answer при обычном направлении, question при reverse.
    hint = None
    if word:
        hint_source = word["question"] if reverse else word_answer_primary
        hint = models.hint_for(hint_source)

    # Ссылки на внешние переводчики — только для english, на само
    # английское слово (word.question), вне зависимости от reverse.
    translate_url = reverso_url = None
    if word and category == "english":
        translate_url = google_translate_url(word["question"])
        reverso_url = reverso_context_url(word["question"])

    return templates.TemplateResponse(
        request,
        "practice.html",
        {
            "categories": CATEGORIES,
            "current_category": category,
            "difficulties": DIFFICULTIES,
            "current_difficulty": difficulty,
            "word": word,
            "word_answer_primary": word_answer_primary,
            "all_answers": all_answers,
            "hint": hint,
            "reverse": reverse,
            "result": result,
            "checked_answer": check_answer,
            "translate_url": translate_url,
            "reverso_url": reverso_url,
            "just_answered": bool(just_answered) and result is True,
        },
    )


@app.post("/practice/skip")
def practice_skip(
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
    record=0 используется автопереходом после ВЕРНОГО ответа —
    там попытка уже записана как 'correct' при самой проверке,
    второй раз (как skip) писать не нужно, иначе одна и та же
    попытка задвоится в статистике.
    """
    if record:
        models.record_attempt(word_id, "skipped", bool(reverse))
    return RedirectResponse(
        url=f"/practice?category={category}&reverse={reverse}&difficulty={difficulty}",
        status_code=303,
    )


@app.post("/practice/accept")
def practice_accept(
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
    Работает только для reverse=0 (question → answer), потому что
    альтернативные варианты хранятся в поле answer, а при reverse=1
    "ответом" технически является question — туда альтернативы
    добавлять не нужно (список синонимов вопроса — не то же самое,
    что альтернативный ответ).
    """
    if not reverse:
        models.add_alternative_answer(word_id, user_answer)
    return RedirectResponse(
        url=f"/practice?category={category}&reverse={reverse}&difficulty={difficulty}",
        status_code=303,
    )


# --- Статистика ---

@app.get("/stats")
def stats(request: Request):
    """
    Показывает сводную статистику по каждой категории (всего попыток,
    точность, отдельно число пропусков) и топ самых сложных слов —
    то, что чаще всего отвечали неверно. Категории без единой
    попытки не отображаются.
    """
    by_category = models.get_stats_by_category()
    hardest = {
        s["category"]: models.get_hardest_words(s["category"])
        for s in by_category
    }
    return templates.TemplateResponse(
        request,
        "stats.html",
        {"by_category": by_category, "hardest": hardest},
    )


# --- Админка: меню категорий + отдельная страница редактирования на каждую ---

@app.get("/admin")
def admin_list(
    request: Request,
    imported: int | None = None,
    skipped: int | None = None,
    purged: int | None = None,
):
    """
    Страница-меню: счётчик слов по каждой категории (ссылка на
    /admin/<category> для редактирования) плюс общие для всего
    словаря функции — добавление, загрузка файлом, экспорт, чистка
    заглушек. Раньше все категории были одной длинной страницей —
    теперь каждая открывается отдельно по кнопке.
    """
    all_words = models.get_all_words()
    counts = {cat: 0 for cat in CATEGORIES}
    for w in all_words:
        counts[w["category"]] = counts.get(w["category"], 0) + 1

    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "categories": CATEGORIES,
            "counts": counts,
            "difficulties": DIFFICULTIES,
            "imported": imported,
            "skipped": skipped,
            "purged": purged,
        },
    )


@app.post("/admin/import")
def admin_import(category: str = Form(...), file: UploadFile = File(...)):
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
    raw_bytes = file.file.read()
    text = raw_bytes.decode("utf-8", errors="ignore")
    filename = (file.filename or "").lower()

    if filename.endswith(".json"):
        quads = models.parse_json_words(text)
        report = models.bulk_add_words_with_extra(category, quads)
    else:
        pairs = models.parse_word_list(text)
        report = models.bulk_add_words(category, pairs)

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
def admin_purge_placeholders():
    """
    Удаляет из всех категорий слова, у которых answer — явная
    заглушка-плейсхолдер (буквально "перевод", "translation" и т.п.
    без реального перевода). Разовая чистка данных, накопившихся до
    того, как импорт начал фильтровать такое сам.
    """
    deleted = models.purge_placeholder_answers()
    return RedirectResponse(url=f"/admin?purged={deleted}", status_code=303)


@app.post("/admin/add")
def admin_add(
    category: str = Form(...),
    question: str = Form(...),
    answer: str = Form(...),
    extra: str = Form(""),
    difficulty: str = Form("medium"),
):
    """
    Обрабатывает отправку формы добавления слова.
    Form(...) означает "это поле обязательно и приходит из HTML-формы".
    category/difficulty валидируются против известных списков (см.
    clean_category/clean_difficulty) — форма и так предлагает только
    правильные варианты, но прямой POST-запрос в обход формы не
    должен пройти с произвольным мусором. question/answer/extra
    обрезаются до разумной длины той же причине.
    Если такой вопрос уже есть в этой категории — тихо не добавляет
    дубликат (та же проверка, что и при импорте файла).
    После сохранения — редирект на страницу этой категории, к самому
    верху (новое слово добавляется в конец списка — прыгать к нему
    незачем, проще просто открыть категорию заново).
    """
    category = clean_category(category)
    difficulty = clean_difficulty(difficulty)
    question = clean_text(question, MAX_QUESTION_LEN)
    answer = clean_text(answer, MAX_ANSWER_LEN)
    extra = clean_text(extra, MAX_EXTRA_LEN)

    if not question or not answer:
        return RedirectResponse(url=f"/admin/{category}", status_code=303)

    existing = models.get_existing_questions(category)
    if question.strip().lower() not in existing:
        models.add_word(category, question, answer, extra, difficulty)
    return RedirectResponse(url=f"/admin/{category}", status_code=303)


@app.post("/admin/edit/{word_id}")
def admin_edit(
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
    После сохранения — редирект на якорь этой же строки
    (#word-<id>), чтобы страница вернулась туда же, где была
    правка, а не наверх.
    """
    category = clean_category(category)
    difficulty = clean_difficulty(difficulty)
    question = clean_text(question, MAX_QUESTION_LEN)
    answer = clean_text(answer, MAX_ANSWER_LEN)
    extra = clean_text(extra, MAX_EXTRA_LEN)

    if not question or not answer:
        return RedirectResponse(url=f"/admin/{category}?per_page={per_page}", status_code=303)

    models.update_word(word_id, category, question, answer, extra, difficulty)
    return RedirectResponse(
        url=f"/admin/{category}?highlight={word_id}&per_page={per_page}#word-{word_id}",
        status_code=303,
    )


@app.post("/admin/delete/{word_id}")
def admin_delete(word_id: int, category: str = Form(...), per_page: str = Form("50")):
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
    if neighbor_id:
        return RedirectResponse(
            url=f"/admin/{category}?highlight={neighbor_id}&per_page={per_page}#word-{neighbor_id}",
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
    Неизвестная категория (не из CATEGORIES) просто покажет пустой
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
            "categories": CATEGORIES,
            "words": words,
            "difficulties": DIFFICULTIES,
            "highlight": highlight,
            "page": page,
            "total_pages": total_pages,
            "per_page": per_page,
            "total": total,
        },
    )
