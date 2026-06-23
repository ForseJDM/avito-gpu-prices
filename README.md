# avito-gpu-helper
Рыночные цены GPU для расширения Avito GPU Helper
Avito GPU Prices
Рыночные цены GPU для расширения Avito GPU Helper.

Формат
{  "version": 1,  "last_updated": "YYYY-MM-DD",  "source": "источник данных",  "prices": [    {      "model": "Точное название модели (должно совпадать с gpu-market-db.js)",      "average_price": число,      "min_safe_price": число,      "max_fair_price": число,      "scam_threshold": число,      "last_updated": "YYYY-MM-DD",      "source": "avito-search-median | manual"    }  ]}
Как обновить цены
Открой prices.json
Обнови нужные значения
Измени last_updated на текущую дату
Сделай commit + push
Расширение подтянет изменения в течение 24 часов
