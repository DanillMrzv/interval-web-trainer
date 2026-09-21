# interval-web-trainer

Веб-приложение для практики английского (словарь и синтаксис), а также Python, Linux и SQL.

## Стек

- Python, FastAPI
- SQLite
- Jinja2-шаблоны

## Запуск

    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    uvicorn main:app --host 127.0.0.1 --port 8000

## Структура

- `main.py`: приложение и маршруты
- `models.py`: модели и работа с базой
- `seed_words.py`: наполнение базы словами
- `templates/`, `static/`: страницы и стили
