# Yandex Maps Parser

Сборщик карточек Яндекс Карт. Маршрут его не импортирует: `rag` читает уже собранный JSON из `SOURCE_PATH`.

## Цепочка

1. `parser.py` открывает карты в браузере и пишет карточки в `иваново.json`. Города берутся из `cities.txt`, по одному на строку. Чекпоинт поиска лежит в `иваново_state.json`.
2. `python parser.py --reviews` дописывает тексты отзывов в `иваново_comments.json`.
3. `python assemble_ivanovo.py` склеивает карточки и отзывы в корневой `Иваново.json`. В этой склейке `type` и `coordinates` пустые.
4. `python fetch_geo.py` открывает карточки ещё раз и дописывает рубрику и координаты. Сейчас путь к файлу зашит как `C:\анечкин проект\Иваново.json`.
5. `python merge_best.py` делает ту же склейку карточек и отзывов, но сохраняет её в `Иваново_best.json` внутри этого каталога и координаты не заполняет.

## Запуск

```bash
pip install playwright
playwright install chromium
python parser.py
python parser.py --reviews
python assemble_ivanovo.py
python fetch_geo.py
```
