# Field GPS Validation — отчёт

- track_id: `20261010-080834`
- реальная поездка (is_real_car_drive): `True`
- фиксаций: 2583

## Допущения (assumptions)

- Точность GPS в исходном треке отсутствовала (рекордер не сохранил HDOP/accuracy). Для replay использовано допущение: **10.0 м** (заполнено для 2583 фиксаций). Это допущение, а не измерение.

## Итоговые метрики

| Метрика | Значение |
|---|---|
| Ожидаемых триггеров (проезд по маршруту в радиусе) | 5 |
| Сработало | 7 |
| Доля ожидаемых сработавших | 1.0 |
| Доля обоснованных срабатываний | 1.0 |
| Повторных срабатываний | 0 |
| Доля отброшенных фиксаций | 0.028 |
| Неопределённых (GPS) случаев | 0 |

Классы POI: passed_near=5, near_crow_flies_but_offroad=6, not_passed=1

Пояснение классов: `passed_near` — трек был на маршруте в радиусе POI; `near_crow_flies_but_offroad` — трек приближался по прямой, но в момент подхода был вне маршрута (движок в режиме съезда не срабатывает); `not_passed` — трек не проходил в радиусе. Несрабатывание не считается ошибкой, если трек не был на маршруте в пределах радиуса.

## По POI

| POI | Радиус, м | Мин. до POI, м | Класс | Триггер | Дист. триггера, м | Точность, м | Раннее/позднее |
|---|---|---|---|---|---|---|---|
| st-001-ganina-yama | 300 | 198.6 | passed_near | да | 296 | 10.0 | within_radius_approaching |
| st-002-shuvakish | 400 | 306.0 | passed_near | да | 398 | 10.0 | within_radius_approaching |
| st-003-viz-stal | 550 | 458.0 | passed_near | да | 547 | 10.0 | within_radius_approaching |
| st-004-dom-brevnovyh | 500 | 50.5 | near_crow_flies_but_offroad | да | 494 | 10.0 | within_radius_approaching |
| st-005-uspensky-sobor | 450 | 226.5 | passed_near | да | 446 | 10.0 | within_radius_approaching |
| st-006-cki-verh-isetsky | 550 | 365.4 | passed_near | да | 547 | 10.0 | within_radius_approaching |
| st-007-centralny-stadion | 850 | 433.3 | near_crow_flies_but_offroad | нет |  |  |  |
| st-008-ioanno-predtechensky | 900 | 675.7 | near_crow_flies_but_offroad | нет |  |  |  |
| st-009-hram-serafima | 600 | 1487.8 | not_passed | нет |  |  |  |
| st-010-memorial-lokomotiv | 1100 | 800.7 | near_crow_flies_but_offroad | нет |  |  |  |
| st-011-muzey-tramvaya | 725 | 236.7 | near_crow_flies_but_offroad | нет |  |  |  |
| st-012-reka-iset | 350 | 200.8 | near_crow_flies_but_offroad | да | 235 | 10.0 | within_radius_approaching |

_Несрабатывание не считается ошибкой, если трек не проходил в пределах радиуса. Отчёт описывает только измеренные данные._
