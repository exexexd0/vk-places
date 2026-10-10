# vk-places

source .venv/bin/activate
pip install -r requirements.txt
docker compose -f deploy/docker-compose.yml up -d

Четыре команды:

python -m rag report
python -m rag rebuild
python -m rag route --query "Хочу поесть и зайти в торговый центр в Иваново"
python -m rag archive
report только фильтрует JSON и печатает статистику. База и нейросеть не вызываются, но переменные в .env всё равно должны существовать. Результат пишется в artifacts/runs/category_report.json.

rebuild заново создаёт векторную таблицу: проверяет размерность эмбеддинга, включает расширение vector, стирает старую таблицу и заливает места пачками по BATCH_SIZE (8). Это платный вызов API эмбеддингов на каждое оставшееся место. Потом строится HNSW-индекс и проверка: число строк равно числу разных org_id, размерность 1024, нет пустых координат и категорий, в metadata нет текстов отзывов.

route строит маршрут. Город можно переопределить: --city Иваново. Ответ печатается и сохраняется в artifacts/runs/route.json. Тот же сценарий есть в cycle_to_determine_POI.ipynb: там тоже нужно сначала сделать rebuild.

archive упаковывает проект в artifacts/archive/ivanovo_rag_<дата>.zip и пишет artifacts/runs/manifest.json. Перед этим проверяет, что route.json валиден, и что строка вида sk-... не попала ни в один файл, кроме .env.
