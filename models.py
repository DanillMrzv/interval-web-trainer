import random
import sqlite3

import os

# Абсолютный путь к файлу БД — рядом с этим файлом (models.py), а НЕ
# относительно текущей рабочей директории процесса. Раньше был просто
# "app.db" (относительный путь): если приложение запущено не из папки
# /opt/englishapp (например, вручную из терминала для отладки, из
# другой папки), sqlite3 тихо создаёт НОВЫЙ пустой файл там, где
# запущен процесс — и кажется, будто все слова и статистика пропали,
# хотя настоящий файл с данными просто лежит в другом месте.
# os.path.dirname(__file__) всегда указывает на папку, где лежит
# models.py, независимо от того, откуда запущена команда.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app.db")


def get_connection():
    """
    Открывает соединение с файлом базы данных app.db.
    Если файла ещё нет — SQLite создаст его автоматически при первой записи.
    check_same_thread=False нужен, потому что FastAPI может обращаться
    к базе из разных потоков (без этого будет ошибка).
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row  # позволяет обращаться к колонкам по имени, а не только по индексу
    return conn


def init_db():
    """
    Создаёт таблицы words и attempts, если их ещё нет, и докатывает
    схему для существующих баз через ALTER TABLE (SQLite не умеет
    "ADD COLUMN IF NOT EXISTS", поэтому проверяем вручную через
    PRAGMA table_info — чтобы повторный запуск на уже обновлённой
    базе не падал с "duplicate column").

    words.difficulty — уровень сложности слова: easy/medium/hard,
    по умолчанию medium для старых записей и для новых, если не
    указано явно.

    attempts.status — заменяет старое булево поле correct.
    Значения: 'correct' / 'incorrect' / 'skipped' (пропуск —
    когда пользователь ушёл на следующее слово, не проверив ответ).
    Старое поле correct оставлено в схеме нетронутым (на случай,
    если где-то в базе уже есть данные) — просто больше не
    используется новым кодом, вся новая статистика пишется в status.
    """
    conn = get_connection()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            extra TEXT,
            UNIQUE(category, question)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            word_id INTEGER NOT NULL,
            correct INTEGER NOT NULL,
            reverse INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (word_id) REFERENCES words(id) ON DELETE CASCADE
        )
    """)

    words_columns = {row["name"] for row in conn.execute("PRAGMA table_info(words)")}
    if "difficulty" not in words_columns:
        conn.execute("ALTER TABLE words ADD COLUMN difficulty TEXT NOT NULL DEFAULT 'medium'")

    attempts_columns = {row["name"] for row in conn.execute("PRAGMA table_info(attempts)")}
    if "status" not in attempts_columns:
        conn.execute("ALTER TABLE attempts ADD COLUMN status TEXT")
        # Заполняем status для уже существующих строк на основе старого correct,
        # чтобы прежняя статистика не потерялась при переходе на новую схему
        conn.execute("UPDATE attempts SET status = CASE WHEN correct = 1 THEN 'correct' ELSE 'incorrect' END WHERE status IS NULL")
    if "shown_text" not in attempts_columns:
        # Что именно было показано как вопрос в момент проверки (конкретный
        # вариант из question/answer, если у слова их несколько) — без этого
        # по одной попытке в БД нельзя понять, что именно человек видел.
        conn.execute("ALTER TABLE attempts ADD COLUMN shown_text TEXT")
    if "typed_answer" not in attempts_columns:
        # Что именно ввёл пользователь — для анализа опечаток/близких
        # промахов, а не только факта "верно/неверно".
        conn.execute("ALTER TABLE attempts ADD COLUMN typed_answer TEXT")

    if "favorite" not in words_columns:
        # Избранное: 1 — слово чаще выпадает в тренажёре (см. /practice в main.py)
        conn.execute("ALTER TABLE words ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0")

    # Таблица словарей (категорий). Раньше список категорий был зашит в
    # main.py — теперь он живёт в базе, чтобы словари можно было
    # создавать из интерфейса.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
    """)
    has_categories = conn.execute("SELECT COUNT(*) AS n FROM categories").fetchone()["n"] > 0
    if not has_categories:
        # Первый запуск после обновления: заводим прежние четыре словаря
        conn.executemany(
            "INSERT OR IGNORE INTO categories (name) VALUES (?)",
            [(name,) for name in DEFAULT_CATEGORIES],
        )
    # Любая категория, у которой уже есть слова, обязана существовать в списке —
    # иначе слова "потеряются" из меню (например, если словарь создан вручную в БД).
    conn.execute(
        "INSERT OR IGNORE INTO categories (name) SELECT DISTINCT category FROM words ORDER BY category"
    )

    conn.commit()
    conn.close()


# --- Словари (категории) ---

DEFAULT_CATEGORIES = ["english", "python", "linux", "sql"]

# Имена, которые нельзя использовать как название словаря: они совпадают
# с путями /admin/<что-то> и перехватывались бы соответствующими роутами.
RESERVED_CATEGORY_NAMES = {"add", "edit", "delete", "import", "export", "purge-placeholders", "categories"}

MAX_CATEGORY_LEN = 30


def normalize_category_name(raw: str) -> str | None:
    """
    Приводит введённое название словаря к безопасному виду: нижний
    регистр, пробелы → "-". Разрешены буквы (в том числе русские),
    цифры, "_" и "-", длина до 30. Иначе возвращает None.
    Название попадает в URL (/admin/<name>, ?category=<name>), поэтому
    спецсимволы вроде &, #, ?, / запрещены заранее.
    """
    import re

    name = re.sub(r"\s+", "-", (raw or "").strip().lower())
    if not name or len(name) > MAX_CATEGORY_LEN:
        return None
    if not re.fullmatch(r"[\w-]+", name):
        return None
    if name in RESERVED_CATEGORY_NAMES:
        return None
    return name


def get_categories() -> list[str]:
    """Список словарей в порядке создания."""
    conn = get_connection()
    rows = conn.execute("SELECT name FROM categories ORDER BY id").fetchall()
    conn.close()
    return [r["name"] for r in rows]


def add_category(raw_name: str) -> tuple[str | None, str]:
    """
    Создаёт новый словарь. Возвращает (имя, статус), где статус:
    'created' — создан; 'exists' — такой уже есть; 'invalid' — имя не
    прошло проверку (тогда имя None).
    """
    name = normalize_category_name(raw_name)
    if name is None:
        return None, "invalid"
    conn = get_connection()
    cur = conn.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
    conn.commit()
    created = cur.rowcount > 0
    conn.close()
    return name, "created" if created else "exists"


def delete_category(name: str) -> bool:
    """
    Удаляет словарь, только если в нём нет слов — чтобы случайным
    кликом нельзя было снести накопленное. Возвращает True, если удалён.
    """
    conn = get_connection()
    n = conn.execute("SELECT COUNT(*) AS n FROM words WHERE category = ?", (name,)).fetchone()["n"]
    if n:
        conn.close()
        return False
    cur = conn.execute("DELETE FROM categories WHERE name = ?", (name,))
    conn.commit()
    deleted = cur.rowcount > 0
    conn.close()
    return deleted


def get_all_words():
    """Возвращает все слова/задания — нужно для страницы /admin (список всего)."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM words ORDER BY category, id").fetchall()
    conn.close()
    return rows


def _difficulty_list(difficulty) -> list[str]:
    """
    Приводит фильтр сложности к списку: None/"" → [] (все уровни),
    строка → [строка], список/кортеж → как есть. Так одни и те же
    функции принимают и один уровень, и несколько сразу.
    """
    if not difficulty:
        return []
    if isinstance(difficulty, str):
        return [difficulty]
    return list(difficulty)


def get_words_for_practice(category: str, difficulty=None):
    """
    То же самое, что get_words_by_category, но с двумя добавленными
    полями на каждое слово — times_shown (сколько раз вообще
    показывалось, любой исход) и times_wrong (сколько раз из них
    ответили неверно). Отдельная функция, а не изменение
    get_words_by_category — та используется ещё и в админке, где эта
    статистика не нужна и только замедлила бы запрос лишним JOIN.

    Используется в main.pick_random_word для выбора следующего слова:
    слова, которые показывались реже (или вообще ни разу), и слова,
    в которых чаще ошибались, должны получать больше веса — эти два
    числа как раз и дают на чём считать такой вес.
    """
    conn = get_connection()
    query = """
        SELECT
            w.*,
            COALESCE(s.times_shown, 0) AS times_shown,
            COALESCE(s.times_wrong, 0) AS times_wrong
        FROM words w
        LEFT JOIN (
            SELECT
                word_id,
                COUNT(*) AS times_shown,
                SUM(CASE WHEN status = 'incorrect' THEN 1 ELSE 0 END) AS times_wrong
            FROM attempts
            GROUP BY word_id
        ) s ON s.word_id = w.id
        WHERE w.category = ?
    """
    params: list = [category]
    levels = _difficulty_list(difficulty)
    if levels:
        query += f" AND w.difficulty IN ({','.join('?' * len(levels))})"
        params.extend(levels)
    query += " ORDER BY w.id"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def get_words_by_category(category: str, difficulty=None, limit: int | None = None, offset: int = 0):
    """
    Возвращает слова нужной категории — нужно для /practice и для
    страницы редактирования категории в админке. Отсортированы по id
    (порядок добавления) — важно для админки: без стабильного порядка
    строки "прыгали" бы местами между запросами, и подсветка/переход
    к соседней строке после удаления не имели бы смысла.
    difficulty — один уровень (easy/medium/hard) или список уровней
    (например, ["easy", "medium"]); пусто — все уровни вместе.
    limit/offset — для постраничной выдачи в админке при большом
    словаре; /practice их не передаёт (там нужны все слова сразу,
    чтобы выбрать случайное).
    """
    conn = get_connection()
    query = "SELECT * FROM words WHERE category = ?"
    params: list = [category]
    levels = _difficulty_list(difficulty)
    if levels:
        query += f" AND difficulty IN ({','.join('?' * len(levels))})"
        params.extend(levels)
    query += " ORDER BY id"
    if limit is not None:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows


def count_words_by_category(category: str, difficulty=None) -> int:
    """Считает слова в категории (с опциональным фильтром сложности) — для пагинации."""
    conn = get_connection()
    query = "SELECT COUNT(*) AS n FROM words WHERE category = ?"
    params: list = [category]
    levels = _difficulty_list(difficulty)
    if levels:
        query += f" AND difficulty IN ({','.join('?' * len(levels))})"
        params.extend(levels)
    row = conn.execute(query, params).fetchone()
    conn.close()
    return row["n"]


def get_word(word_id: int):
    """Возвращает одно слово по id — нужно для формы редактирования."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM words WHERE id = ?", (word_id,)).fetchone()
    conn.close()
    return row


def add_word(category: str, question: str, answer: str, extra: str = "", difficulty: str = "medium"):
    """Добавляет новое слово/задание в базу и возвращает его id."""
    conn = get_connection()
    cur = conn.execute(
        "INSERT INTO words (category, question, answer, extra, difficulty) VALUES (?, ?, ?, ?, ?)",
        (category, question, answer, extra, difficulty or "medium"),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_word(word_id: int, category: str, question: str, answer: str, extra: str = "", difficulty: str = "medium"):
    """Изменяет существующее слово/задание по id."""
    conn = get_connection()
    conn.execute(
        "UPDATE words SET category = ?, question = ?, answer = ?, extra = ?, difficulty = ? WHERE id = ?",
        (category, question, answer, extra, difficulty or "medium", word_id),
    )
    conn.commit()
    conn.close()


def delete_word(word_id: int):
    """Удаляет слово/задание по id."""
    conn = get_connection()
    conn.execute("DELETE FROM words WHERE id = ?", (word_id,))
    conn.commit()
    conn.close()


def toggle_favorite(word_id: int) -> bool | None:
    """
    Переключает флаг "избранное" у слова и возвращает новое состояние
    (True — теперь в избранном). None, если слова с таким id нет.
    """
    conn = get_connection()
    row = conn.execute("SELECT favorite FROM words WHERE id = ?", (word_id,)).fetchone()
    if row is None:
        conn.close()
        return None
    new_value = 0 if row["favorite"] else 1
    conn.execute("UPDATE words SET favorite = ? WHERE id = ?", (new_value, word_id))
    conn.commit()
    conn.close()
    return bool(new_value)


def get_neighbor_word_id(word_id: int, category: str) -> int | None:
    """
    Находит id "соседнего" слова той же категории — следующего по
    порядку id, а если удаляемое было последним в списке — предыдущего.
    Вызывается ДО удаления, чтобы после него можно было подсветить/
    проскроллить туда, где было удалённое слово, вместо того чтобы
    страница просто открывалась заново с самого верха.
    Возвращает None, если слово было единственным в категории.
    """
    conn = get_connection()
    next_row = conn.execute(
        "SELECT id FROM words WHERE category = ? AND id > ? ORDER BY id LIMIT 1",
        (category, word_id),
    ).fetchone()
    if next_row:
        conn.close()
        return next_row["id"]

    prev_row = conn.execute(
        "SELECT id FROM words WHERE category = ? AND id < ? ORDER BY id DESC LIMIT 1",
        (category, word_id),
    ).fetchone()
    conn.close()
    return prev_row["id"] if prev_row else None


# --- Альтернативные ответы ---
# Несколько принимаемых вариантов перевода хранятся в одном поле answer,
# разделённые " ; ". Первый вариант — "основной" (показывается как ответ
# при реверсе и при "показать ответ"), остальные — тоже засчитываются
# как верные при проверке, но не показываются как единственно правильные.

ANSWER_SEP = ";"


def split_answers(answer_field: str) -> list[str]:
    """
    Разбивает поле answer на список отдельных принимаемых вариантов.
    Разделителем считается ";" (используется при явном добавлении
    альтернативы через кнопку в тренажёре) ИЛИ "," (используется в
    словаре как естественный список синонимов: "дуться, быть в
    плохом настроении" — это два принимаемых варианта, не один
    цельный ответ).

    Запятая внутри скобок не считается разделителем — "большое
    (объект/чувство)" остаётся одним вариантом, а не режется на
    "большое" и "(объект/чувство)". Это нужно, потому что в части
    словаря запятая используется и просто для уточнения внутри
    одного варианта.
    """
    import re

    # Сначала делим по ";", затем каждую часть — по "," вне скобок
    variants = []
    for chunk in answer_field.split(ANSWER_SEP):
        # split по запятой, но не внутри () — учитываем глубину скобок вручную
        depth = 0
        current = ""
        pieces = []
        for ch in chunk:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth = max(0, depth - 1)
            if ch == "," and depth == 0:
                pieces.append(current)
                current = ""
            else:
                current += ch
        pieces.append(current)
        variants.extend(pieces)

    return [v.strip() for v in variants if v.strip()]


def primary_answer(answer_field: str) -> str:
    """Первый (основной) вариант — используется для показа и для реверса."""
    variants = split_answers(answer_field)
    return variants[0] if variants else answer_field


def hint_for(text: str) -> str:
    """
    Возвращает подсказку — первые N символов text, где N растёт
    вместе с длиной слова: короче слово — меньше букв, чтобы
    подсказка не выдавала ответ целиком.
        длина ≤ 4  → 1 буква
        длина 5-8  → 2 буквы
        длина > 8  → 3 буквы
    Многословные ответы (несколько слов через пробел) считаются по
    длине первого слова — раскрывать сразу несколько слов подряд
    было бы слишком щедрой подсказкой.
    """
    text = text.strip()
    if not text:
        return ""
    first_word_len = len(text.split()[0])
    if first_word_len <= 4:
        n = 1
    elif first_word_len <= 8:
        n = 2
    else:
        n = 3
    return text[:n]


def _normalize_for_comparison(text: str) -> str:
    """
    Приводит текст к виду для сравнения ответов: нижний регистр,
    без пробелов по краям, и "ё" заменена на "е" — в неформальной
    письменной речи люди часто печатают "е" вместо "ё" (особенно
    на телефоне), это не должно считаться ошибкой.
    """
    return text.strip().lower().replace("ё", "е")


def is_correct_answer(user_input: str, answer_field: str) -> bool:
    """Проверяет введённый ответ против ВСЕХ принимаемых вариантов."""
    key = _normalize_for_comparison(user_input)
    return key in {_normalize_for_comparison(a) for a in split_answers(answer_field)}


def _add_variant(word_id: int, column: str, new_variant: str):
    """
    Общая логика для add_alternative_answer/add_alternative_question:
    добавляет новый вариант в поле question или answer, если такого
    варианта там ещё нет (сравнение без учёта регистра). column должен
    быть "question" или "answer" — имя жёстко проверяется, а не
    подставляется напрямую в SQL, чтобы не открыть SQL-инъекцию через
    построение запроса с f-строкой.
    """
    if column not in ("question", "answer"):
        raise ValueError("column must be 'question' or 'answer'")
    word = get_word(word_id)
    if word is None:
        return
    variants = split_answers(word[column])
    if new_variant.strip().lower() not in {v.lower() for v in variants}:
        variants.append(new_variant.strip())
    updated = f" {ANSWER_SEP} ".join(variants)
    conn = get_connection()
    conn.execute(f"UPDATE words SET {column} = ? WHERE id = ?", (updated, word_id))
    conn.commit()
    conn.close()


def add_alternative_answer(word_id: int, new_variant: str):
    """
    Добавляет новый вариант перевода (answer) к существующему слову.
    Используется в тренажёре в обычном направлении, когда пользователь
    ответил "неверно", но хочет засчитать свой перевод как верный.
    """
    _add_variant(word_id, "answer", new_variant)


def add_alternative_question(word_id: int, new_variant: str):
    """
    Добавляет новый вариант исходного слова (question) — например,
    если в словаре записано "seller → продавец", а пользователь в
    реверс-режиме ввёл "salesman", это тоже верно: добавляем
    "salesman" вторым принимаемым вариантом question, чтобы дальше
    оба слова засчитывались как правильный ответ на "продавец"
    (аналогично metro/tube/underground для одного и того же перевода).
    """
    _add_variant(word_id, "question", new_variant)


# --- Статистика попыток ---

def record_attempt(word_id: int, status: str, reverse: bool, shown_text: str | None = None, typed_answer: str | None = None):
    """
    Сохраняет одну попытку — сырые данные для статистики и дальнейшего
    анализа. status: 'correct' / 'incorrect' / 'skipped'. 'skipped'
    пишется, когда пользователь переходит к следующему слову, не
    отправив проверку ответа (см. /practice/skip в main.py).

    shown_text — какой именно вариант вопроса был показан (если у
    слова несколько синонимов через ; или ,), typed_answer — что
    именно ввёл пользователь. Оба необязательны (для 'skipped'
    осмысленного typed_answer нет) — нужны, чтобы потом можно было
    посмотреть не только "верно/неверно", но и что конкретно человек
    печатал, вплоть до опечаток.

    Поле correct тоже заполняется (1 для correct, иначе 0) — только
    для совместимости со старым кодом, если он где-то ещё читает
    это поле напрямую; новый код всегда должен использовать status.
    """
    conn = get_connection()
    conn.execute(
        "INSERT INTO attempts (word_id, correct, reverse, status, shown_text, typed_answer) VALUES (?, ?, ?, ?, ?, ?)",
        (word_id, int(status == "correct"), int(reverse), status, shown_text, typed_answer),
    )
    conn.commit()
    conn.close()


def get_stats_by_category() -> list[dict]:
    """
    Возвращает сводную статистику по каждой категории: всего попыток,
    верных, неверных, пропущенных, точность (%, считается только от
    проверенных ответов — correct + incorrect, пропуски в знаменатель
    точности не входят, так как это не "ответ неверный", а "ответа не
    было"). Категории без единой попытки не включаются.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            w.category AS category,
            COUNT(*) AS total,
            SUM(CASE WHEN a.status = 'correct' THEN 1 ELSE 0 END) AS correct,
            SUM(CASE WHEN a.status = 'incorrect' THEN 1 ELSE 0 END) AS incorrect,
            SUM(CASE WHEN a.status = 'skipped' THEN 1 ELSE 0 END) AS skipped
        FROM attempts a
        JOIN words w ON w.id = a.word_id
        GROUP BY w.category
        ORDER BY w.category
    """).fetchall()
    conn.close()
    result = []
    for r in rows:
        correct = r["correct"] or 0
        incorrect = r["incorrect"] or 0
        skipped = r["skipped"] or 0
        checked = correct + incorrect  # знаменатель для точности, без пропусков
        result.append({
            "category": r["category"],
            "total": r["total"],
            "correct": correct,
            "incorrect": incorrect,
            "skipped": skipped,
            "accuracy": round(100 * correct / checked) if checked else 0,
        })
    return result


def get_hardest_words(category: str, limit: int = 10) -> list[dict]:
    """
    Возвращает слова с наибольшим числом неверных попыток в категории —
    то, что стоит повторить в первую очередь. Пропуски не считаются
    ошибкой при сортировке (сортируем по incorrect, не по incorrect+skipped),
    но показываются отдельным столбцом для полноты картины.
    Слова без единой проверенной попытки не включаются.
    """
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            w.id, w.question, w.answer,
            COUNT(*) AS total,
            SUM(CASE WHEN a.status = 'correct' THEN 1 ELSE 0 END) AS correct,
            SUM(CASE WHEN a.status = 'incorrect' THEN 1 ELSE 0 END) AS incorrect,
            SUM(CASE WHEN a.status = 'skipped' THEN 1 ELSE 0 END) AS skipped
        FROM attempts a
        JOIN words w ON w.id = a.word_id
        WHERE w.category = ?
        GROUP BY w.id
        HAVING (correct + incorrect) > 0
        ORDER BY incorrect DESC, total DESC
        LIMIT ?
    """, (category, limit)).fetchall()
    conn.close()
    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "question": r["question"],
            "answer": r["answer"],
            "total": r["total"],
            "correct": r["correct"] or 0,
            "incorrect": r["incorrect"] or 0,
            "skipped": r["skipped"] or 0,
        })
    return result


def get_existing_questions(category: str) -> set[str]:
    """
    Возвращает множество уже существующих вопросов (в нижнем регистре,
    без пробелов по краям) для категории — используется для проверки
    дублей ДО вставки, чтобы явно посчитать пропущенные.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT question FROM words WHERE category = ?", (category,)
    ).fetchall()
    conn.close()
    return {r["question"].strip().lower() for r in rows}


def bulk_add_words(category: str, pairs: list[tuple[str, str]], difficulty: str = "medium") -> dict:
    """
    Добавляет много слов пачкой, пропуская дубликаты.
    pairs — список (question, answer).
    difficulty применяется ко ВСЕМ словам пачки — текстовый формат
    импорта не описывает сложность построчно, поэтому она задаётся
    один раз для всего файла (выбор в форме загрузки).
    Дубликат — вопрос, который уже есть в этой категории (без учёта
    регистра/пробелов), ИЛИ повторяется дважды внутри самого pairs.
    Возвращает {"added": N, "skipped": M, "skipped_words": [...]}.
    """
    existing = get_existing_questions(category)
    to_add = []
    skipped = []

    for question, answer in pairs:
        key = question.strip().lower()
        if not question.strip() or not answer.strip():
            continue  # пустые строки просто игнорируем, не считаем как skipped
        if key in existing:
            skipped.append(question.strip())
            continue
        existing.add(key)  # чтобы поймать и повтор внутри самого файла
        to_add.append((category, question.strip(), answer.strip(), "", difficulty or "medium"))

    if to_add:
        conn = get_connection()
        conn.executemany(
            "INSERT INTO words (category, question, answer, extra, difficulty) VALUES (?, ?, ?, ?, ?)",
            to_add,
        )
        conn.commit()
        conn.close()

    return {"added": len(to_add), "skipped": len(skipped), "skipped_words": skipped}


def parse_word_list(text: str) -> list[tuple[str, str]]:
    """
    Разбирает "грязный" текст со словами в пары (question, answer).
    Для каждой строки пробует по очереди:

    1) Явный разделитель -, —, : или таб (самый надёжный сигнал,
       используется первым, если есть):
           despite - не смотря на
           despite: не смотря на
           despite<TAB>не смотря на
       Дефис/тире считается разделителем только если с пробелом хотя бы
       с одной стороны — иначе слово "что-то" не сломается пополам.

    2) Если явного разделителя нет — ищет точку, где строка переходит
       с латиницы на кириллицу (или наоборот), и режет по ней. Это
       устойчиво даже к многословным ответам:
           grape виноград                      → grape / виноград
           vast что-то большое / крупное       → vast / что-то большое / крупное
           precipice down the cliff обрыв      → "precipice down the cliff" / обрыв
       (весь латинский кусок — question, весь кириллический — answer).

    Строки, где не удалось определить оба алфавита (например, строка
    только на одном языке, или вообще без букв), и пустые строки —
    пропускаются молча, не считаются ошибкой.
    """
    import re

    pairs = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        question = answer = None

        # Шаг 1: явный разделитель
        match = re.search(r"(?:^|\s)[-—](?=\s)|(?<=\s)[-—](?:\s|$)|[:\t]", line)
        if match:
            question = line[: match.start()].strip()
            answer = line[match.end():].strip()
        else:
            # Шаг 2: находим границу латиница <-> кириллица
            boundary = re.search(r"[A-Za-z][^A-Za-zА-Яа-яЁё]*(?=[А-Яа-яЁё])", line)
            if boundary is None:
                boundary = re.search(r"[А-Яа-яЁё][^A-Za-zА-Яа-яЁё]*(?=[A-Za-z])", line)
                if boundary:
                    # строка начинается с русского, потом английский —
                    # редкий случай (реверс-формат), обрабатываем так же
                    answer = line[: boundary.end()].strip()
                    question = line[boundary.end():].strip()
            else:
                cut = boundary.end()
                question = line[:cut].strip()
                answer = line[cut:].strip()

        if question and answer:
            pairs.append((question, answer))
    return pairs


def parse_json_words(text: str) -> list[tuple[str, str, str, str]]:
    """
    Разбирает JSON-словарь вида:
        [
          {"word": "car", "translation": "машина", "transcription": "[kɑː]",
           "difficulty": "easy"},
          ...
        ]
    в список (question, answer, extra, difficulty).

    Понимает небольшие вариации в названиях полей на случай, если
    в разных файлах они называются чуть иначе:
        word / question / en / english  → question
        translation / answer / ru / russian / перевод → answer
        transcription / extra / примечание → extra
        difficulty / level / сложность → difficulty (easy/medium/hard,
            любое нераспознанное или отсутствующее значение → medium)

    Записи без word/question или без translation/answer пропускаются,
    как и записи, где answer — явная заглушка-плейсхолдер (само слово
    "перевод", "translation", "TODO" и т.п. без реального перевода) —
    такое иногда попадает в файлы при неаккуратном заполнении вручную
    и не несёт пользы в тренажёре.
    """
    import json

    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []

    if not isinstance(data, list):
        return []

    question_keys = ("word", "question", "en", "english")
    answer_keys = ("translation", "answer", "ru", "russian", "перевод")
    extra_keys = ("transcription", "extra", "примечание", "note")
    difficulty_keys = ("difficulty", "level", "сложность")
    valid_difficulties = {"easy", "medium", "hard"}
    placeholder_answers = {"перевод", "translation", "todo", "tbd", "???", "?"}

    triples = []
    for item in data:
        if not isinstance(item, dict):
            continue

        question = next((item[k] for k in question_keys if item.get(k)), None)
        answer = next((item[k] for k in answer_keys if item.get(k)), None)
        extra = next((item[k] for k in extra_keys if item.get(k)), "")
        difficulty_raw = next((item[k] for k in difficulty_keys if item.get(k)), "medium")
        difficulty = str(difficulty_raw).strip().lower()
        if difficulty not in valid_difficulties:
            difficulty = "medium"

        if not question or not answer:
            continue
        if str(answer).strip().lower() in placeholder_answers:
            continue

        triples.append((
            str(question).strip(),
            str(answer).strip(),
            str(extra).strip(),
            difficulty,
        ))

    return triples


def bulk_add_words_with_extra(category: str, quads: list[tuple[str, str, str, str]]) -> dict:
    """
    Как bulk_add_words, но каждая запись — четвёрка
    (question, answer, extra, difficulty). Используется JSON-импортом.
    Логика дублей и вставки такая же, как в bulk_add_words — отдельная
    функция вместо усложнения сигнатуры основной, которую использует
    и обычный .txt импорт (там нет ни extra, ни difficulty на входе).
    """
    existing = get_existing_questions(category)
    to_add = []
    skipped = []

    for question, answer, extra, difficulty in quads:
        key = question.strip().lower()
        if not question.strip() or not answer.strip():
            continue
        if key in existing:
            skipped.append(question.strip())
            continue
        existing.add(key)
        to_add.append((category, question.strip(), answer.strip(), extra.strip(), difficulty or "medium"))

    if to_add:
        conn = get_connection()
        conn.executemany(
            "INSERT INTO words (category, question, answer, extra, difficulty) VALUES (?, ?, ?, ?, ?)",
            to_add,
        )
        conn.commit()
        conn.close()

    return {"added": len(to_add), "skipped": len(skipped), "skipped_words": skipped}


# --- Экспорт словаря ---

def export_words_json(category: str | None = None) -> str:
    """
    Выгружает словарь в JSON — тот же формат полей, что принимает
    parse_json_words, чтобы экспорт можно было потом заливать обратно
    как импорт (например, после переноса на другой сервер, или как
    резервная копия). Если category не указана — выгружает все
    категории сразу, добавляя поле "category" в каждую запись.
    """
    import json

    if category:
        words = get_words_by_category(category)
        items = [
            {
                "word": w["question"],
                "translation": w["answer"],
                "transcription": w["extra"] or "",
                "difficulty": w["difficulty"],
            }
            for w in words
        ]
    else:
        words = get_all_words()
        items = [
            {
                "category": w["category"],
                "word": w["question"],
                "translation": w["answer"],
                "transcription": w["extra"] or "",
                "difficulty": w["difficulty"],
            }
            for w in words
        ]

    return json.dumps(items, ensure_ascii=False, indent=2)


def purge_placeholder_answers(category: str | None = None) -> int:
    """
    Удаляет слова, у которых answer — явная заглушка-плейсхолдер
    (буквально "перевод", "translation" и т.п. без реального
    перевода) — то, что могло попасть в базу до того, как
    parse_json_words начал такое фильтровать при импорте.
    Возвращает число удалённых записей. Если category не указана —
    чистит все категории.
    """
    placeholder_answers = {"перевод", "translation", "todo", "tbd", "???", "?"}
    conn = get_connection()
    if category:
        rows = conn.execute(
            "SELECT id, answer FROM words WHERE category = ?", (category,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id, answer FROM words").fetchall()

    to_delete = [
        r["id"] for r in rows
        if r["answer"].strip().lower() in placeholder_answers
    ]

    if to_delete:
        conn.executemany("DELETE FROM words WHERE id = ?", [(i,) for i in to_delete])
        conn.commit()
    conn.close()
    return len(to_delete)


# --- Интересные факты (баннер на главной) ---
#
# Пока это просто фиксированный список для обкатки фичи — темы вперемешку
# (английский/python/linux/sql/общее), без привязки к конкретному словарю.
# Если приживётся, можно будет перенести в таблицу в БД и добавить
# управление через админку, как со словами — но это уже следующий шаг.
FACTS = [
    "Python назван не в честь змеи, а в честь британского комедийного шоу «Monty Python's Flying Circus» — Гвидо ван Россум был его фанатом.",
    "Первый компьютерный «баг» был в буквальном смысле насекомым: в 1947 году в реле гарвардского Mark II нашли застрявшую моль.",
    "Команда ls в Unix изначально задумывалась как «list», но её сократили — на телетайпах 1970-х каждое лишнее нажатие клавиши стоило времени.",
    "SQL изначально назывался SEQUEL (Structured English Query Language), но название пришлось сократить из-за торговой марки другой компании.",
    "Ядро Linux сегодня весит больше 30 миллионов строк кода, а первая версия 1991 года умещалась в несколько тысяч строк.",
    "Слово «робот» придумал не инженер, а писатель — чешский драматург Карел Чапек ввёл его в пьесе 1920 года.",
    "В английском языке буква E встречается чаще всех остальных — поэтому частотный анализ при взломе простых шифров начинают именно с неё.",
    "Первая версия HTML, придуманная Тимом Бернерсом-Ли в 1991 году, содержала всего 18 тегов.",
    "Слово «computer» до середины XX века означало не машину, а человека — так называли людей, которые вручную считали вычисления.",
    "Git был написан Линусом Торвальдсом всего за пару недель в 2005 году — как экстренная замена коммерческой системе BitKeeper.",
    "В базах данных NULL — это не «ноль» и не «пусто», а буквально «неизвестно»; поэтому выражение NULL = NULL возвращает не TRUE, а тоже NULL.",
    "Слово «byte» придумали специально непохожим на «bit» — чтобы их не путали на слух при разговоре или по телефону.",
    "Название оболочки bash — каламбур: Bourne-Again SHell, отсылка и к автору более раннего sh (Стивену Борну), и к выражению «born again».",
    "Самый первый в мире веб-сайт, info.cern.ch, до сих пор работает и доступен по своему оригинальному адресу.",
    "В английском языке больше слов начинается на букву S, чем на любую другую букву алфавита.",
    "Флаг --force в git — не волшебная кнопка «исправить всё», а буквально «перезаписать историю, не спрашивая»: им легко случайно затереть чужие коммиты.",
    "Термин «cookie» для веб-браузеров придумали по аналогии со старым программистским понятием «magic cookie» — небольшим куском данных, которым обменивались программы.",
    "Слово «despite» и предлог «spite» исторически не связаны с обидой напрямую — оба восходят к латинскому «despicere», «смотреть свысока».",
]


def random_fact() -> str:
    """Случайный факт для баннера на главной странице."""
    return random.choice(FACTS)
