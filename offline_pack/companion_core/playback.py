# -*- coding: utf-8 -*-
"""GPS playback engine приложения-компаньона.

UI-независимое ядро: принимает поток GPS-фиксаций, выдаёт события.
МОДУЛЬ БЕЗ СЕТИ: только math/dataclasses/json. Переносимо в Kotlin 1:1 по логике.

Правила срабатывания истории (не только «вход в круг»):
- фиксация должна пройти фильтр точности;
- прямое расстояние до POI <= trigger_radius_m истории;
- поездка приближается к позиции истории вдоль маршрута (прогресс растёт
  к story.route_distance_m, а не прошёл мимо);
- поездка не в состоянии OFF_ROUTE;
- история в статусе PENDING (однократность) + защитный кулдаун.
"""
import json
import math
from dataclasses import dataclass, field


# ----------------------------- геометрия ------------------------------------

def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class RouteGeometry:
    """Расстояние до полилинии и прогресс вдоль маршрута (равноугольная проекция)."""

    def __init__(self, coords_latlon):
        if len(coords_latlon) < 2:
            raise ValueError("маршрут должен содержать >=2 точек")
        lat0 = sum(c[0] for c in coords_latlon) / len(coords_latlon)
        self.kx = math.cos(math.radians(lat0)) * 111320.0
        self.ky = 110540.0
        self.lon0 = sum(c[1] for c in coords_latlon) / len(coords_latlon)
        self.lat0 = lat0
        self.pts = [self._xy(lat, lon) for lat, lon in coords_latlon]
        self.cum = [0.0]
        for (x1, y1), (x2, y2) in zip(self.pts, self.pts[1:]):
            self.cum.append(self.cum[-1] + math.hypot(x2 - x1, y2 - y1))
        self.total_m = self.cum[-1]

    def _xy(self, lat, lon):
        return (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky

    def measure(self, lat, lon):
        """(расстояние до маршрута, прогресс вдоль маршрута от старта)."""
        x, y = self._xy(lat, lon)
        best_d, best_along = float("inf"), 0.0
        for i, ((x1, y1), (x2, y2)) in enumerate(zip(self.pts, self.pts[1:])):
            dx, dy = x2 - x1, y2 - y1
            l2 = dx * dx + dy * dy
            t = 0.0 if l2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / l2))
            px, py = x1 + t * dx, y1 + t * dy
            d = math.hypot(x - px, y - py)
            if d < best_d:
                best_d = d
                best_along = self.cum[i] + t * math.sqrt(l2)
        return best_d, best_along

    def point_at(self, progress_m, offset_m=0.0):
        """(lat, lon) на полилинии на заданном прогрессе со смещением
        перпендикулярно маршруту (для симуляции съездов)."""
        progress_m = max(0.0, min(self.total_m, progress_m))
        i = 0
        while i < len(self.cum) - 2 and self.cum[i + 1] < progress_m:
            i += 1
        seg_len = self.cum[i + 1] - self.cum[i]
        t = 0.0 if seg_len == 0 else (progress_m - self.cum[i]) / seg_len
        (x1, y1), (x2, y2) = self.pts[i], self.pts[i + 1]
        x, y = x1 + (x2 - x1) * t, y1 + (y2 - y1) * t
        # единичная нормаль сегмента
        dx, dy = x2 - x1, y2 - y1
        n = math.hypot(dx, dy) or 1.0
        x += offset_m * (-dy / n)
        y += offset_m * (dx / n)
        lon = x / self.kx + self.lon0
        lat = y / self.ky + self.lat0
        return lat, lon


# ------------------------------ фиксации ------------------------------------

@dataclass
class Fix:
    lat: float
    lon: float
    accuracy_m: float = 20.0        # точность GPS-фиксации (радиус 68%)
    timestamp_s: float = 0.0


# ------------------------------- конфиг -------------------------------------

@dataclass
class TriggerConfig:
    min_gps_accuracy_m: float = 50.0     # хуже — фиксация отбрасывается
    off_route_threshold_m: float = 250.0 # дальше — считаем съездом
    off_route_fixes: int = 3             # подряд, чтобы объявить съезд
    retrigger_cooldown_s: float = 600.0  # защита от мгновенного повтора
    max_jump_m_per_s: float = 120.0      # телепорт-фильтр (~430 км/ч)
    corridor_radius_m: float = 2000.0    # коридор маршрута (из манифеста)


# --------------------------- состояния истории ------------------------------

ST_PENDING = "PENDING"
ST_TRIGGERED = "TRIGGERED"     # история показана (или передана UI)
ST_COMPLETED = "COMPLETED"     # UI подтвердил воспроизведение до конца
ST_SKIPPED = "SKIPPED"         # пассажир пропустил
ST_MISSED = "MISSED"           # проехали мимо без срабатывания


@dataclass
class StoryRuntime:
    story_id: str
    poi_id: str
    lat: float
    lon: float
    route_distance_m: float
    trigger_radius_m: float
    status: str = ST_PENDING
    last_trigger_ts: float = -1e18


# -------------------------------- события -----------------------------------

EV_FIX_REJECTED = "FIX_REJECTED"
EV_TRIGGER = "STORY_TRIGGER"
EV_MISSED = "STORY_MISSED"
EV_OFF_ROUTE = "TRIP_OFF_ROUTE"
EV_RESUME = "TRIP_RESUMED"
EV_TRIP_END = "TRIP_COMPLETED"


@dataclass
class TripPlayer:
    bundle: object
    config: TriggerConfig = field(default_factory=TriggerConfig)

    def __post_init__(self):
        self.geometry = RouteGeometry(self.bundle.route_coordinates)
        self.stories = [
            StoryRuntime(
                story_id=s["story_id"], poi_id=s["poi_id"],
                lat=s["coordinates"]["lat"], lon=s["coordinates"]["lon"],
                route_distance_m=s["route_distance_m"],
                trigger_radius_m=s["trigger_radius_m"],
            )
            for s in self.bundle.stories
        ]
        self.stories.sort(key=lambda r: r.route_distance_m)
        self.fix_count = 0
        self.rejected_count = 0
        self._off_streak = 0
        self.off_route = False
        self.last_progress_m = None
        self.last_ts = None
        self.events_log = []

    # ------------------------- обработка фиксации ---------------------------

    def process_fix(self, fix: Fix):
        events = []

        if not (-90 <= fix.lat <= 90 and -180 <= fix.lon <= 180):
            self.rejected_count += 1
            return self._emit(events, EV_FIX_REJECTED, reason="coords_out_of_range")
        if fix.accuracy_m > self.config.min_gps_accuracy_m:
            self.rejected_count += 1
            return self._emit(events, EV_FIX_REJECTED, reason="low_accuracy",
                             accuracy_m=fix.accuracy_m)
        if self.last_ts is not None and fix.timestamp_s <= self.last_ts:
            self.rejected_count += 1
            return self._emit(events, EV_FIX_REJECTED, reason="non_monotonic_time")
        dist, progress = self.geometry.measure(fix.lat, fix.lon)
        if self.last_progress_m is not None and self.last_ts is not None:
            dt = max(fix.timestamp_s - self.last_ts, 1e-6)
            jump = abs(progress - self.last_progress_m)
            if jump / dt > self.config.max_jump_m_per_s:
                self.rejected_count += 1
                return self._emit(events, EV_FIX_REJECTED, reason="teleport_jump")

        self.fix_count += 1
        self.last_progress_m = progress
        self.last_ts = fix.timestamp_s

        # --- съезд с маршрута / возвращение ---
        if dist > self.config.off_route_threshold_m:
            self._off_streak += 1
            if not self.off_route and self._off_streak >= self.config.off_route_fixes:
                self.off_route = True
                self._emit(events, EV_OFF_ROUTE, distance_m=round(dist),
                           progress_m=round(progress))
        else:
            self._off_streak = 0
            if self.off_route:
                self.off_route = False
                self._emit(events, EV_RESUME, distance_m=round(dist),
                           progress_m=round(progress))

        # --- пропуск историй, которые уже проехали ---
        if not self.off_route:
            for st in self.stories:
                if (st.status == ST_PENDING
                        and progress > st.route_distance_m + st.trigger_radius_m):
                    st.status = ST_MISSED
                    self._emit(events, EV_MISSED, story_id=st.story_id,
                               progress_m=round(progress))

            # --- срабатывание ---
            if not self.off_route:
                for st in self.stories:
                    if st.status != ST_PENDING:
                        continue
                    if fix.timestamp_s - st.last_trigger_ts < self.config.retrigger_cooldown_s:
                        continue
                    direct = haversine_m(fix.lat, fix.lon, st.lat, st.lon)
                    if direct > st.trigger_radius_m:
                        continue
                    # приближение: ещё не проехали позицию истории
                    if progress > st.route_distance_m + st.trigger_radius_m * 0.5:
                        continue
                    st.status = ST_TRIGGERED
                    st.last_trigger_ts = fix.timestamp_s
                    self._emit(events, EV_TRIGGER, story_id=st.story_id,
                               poi_id=st.poi_id, direct_distance_m=round(direct),
                               progress_m=round(progress))

        if progress >= self.geometry.total_m - 50:
            self._emit(events, EV_TRIP_END, progress_m=round(progress))
        return events

    # ------------------------- управление историями -------------------------

    def mark_completed(self, story_id):
        for st in self.stories:
            if st.story_id == story_id:
                st.status = ST_COMPLETED

    def mark_skipped(self, story_id):
        for st in self.stories:
            if st.story_id == story_id:
                st.status = ST_SKIPPED

    # ----------------------------- состояние --------------------------------

    def export_state(self):
        """Сериализация состояния поездки (для повторного открытия)."""
        return {
            "fix_count": self.fix_count,
            "rejected_count": self.rejected_count,
            "off_route": self.off_route,
            "off_streak": self._off_streak,
            "last_progress_m": self.last_progress_m,
            "last_ts": self.last_ts,
            "stories": [
                {"story_id": st.story_id, "status": st.status,
                 "last_trigger_ts": st.last_trigger_ts}
                for st in self.stories
            ],
        }

    def restore_state(self, state):
        self.fix_count = state.get("fix_count", 0)
        self.rejected_count = state.get("rejected_count", 0)
        self.off_route = state.get("off_route", False)
        self._off_streak = state.get("off_streak", 0)
        self.last_progress_m = state.get("last_progress_m")
        self.last_ts = state.get("last_ts")
        by_id = {st.story_id: st for st in self.stories}
        for rec in state.get("stories", []):
            st = by_id.get(rec.get("story_id"))
            if st:
                st.status = rec.get("status", ST_PENDING)
                st.last_trigger_ts = rec.get("last_trigger_ts", -1e18)

    def _emit(self, events, ev_type, **payload):
        ev = {"type": ev_type, "fix_index": self.fix_count, **payload}
        events.append(ev)
        self.events_log.append(ev)
        return events

    def status_summary(self):
        from collections import Counter
        return dict(Counter(st.status for st in self.stories))
