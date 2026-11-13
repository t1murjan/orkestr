"""Demo mode: a small seeded team, cleanup, and a driver that keeps it busy."""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from .interfaces import EngineError

log = logging.getLogger("orkestr.demo")

LEAD_NAME = "Тимлид"

# name, role, model, color, x, y, supervisor name
_TEAM: list[tuple[str, str, str, str, int, int, str | None]] = [
    (LEAD_NAME, "Руководит командой: разбивает задачу на части и собирает итог", "demo-cloud", "a", 490, 40, None),
    ("Архитектор", "Проектирует структуру решения и выбирает подход", "demo-cloud", "b", 100, 240, LEAD_NAME),
    ("Бэкенд-разработчик", "Пишет серверный код, API и работу с базой данных", "demo-local-large", "c", 360, 240, LEAD_NAME),
    ("Фронтенд-разработчик", "Делает интерфейс: компоненты, вёрстка, состояние", "demo-local", "d", 620, 240, LEAD_NAME),
    ("Тестировщик", "Пишет и запускает тесты, ищет регрессии", "demo-local", "e", 880, 240, LEAD_NAME),
    ("Ревьюер", "Проверяет изменения на ошибки и соответствие стилю", "demo-cloud", "f", 360, 440, "Бэкенд-разработчик"),
]

DEMO_TASKS = [
    "Добавить в сервис заказов эндпоинт экспорта в CSV и покрыть его тестами",
    "Исправить падение страницы профиля при пустом списке уведомлений",
    "Перевести модуль авторизации на токены с ограниченным сроком жизни",
    "Ускорить загрузку главной страницы: найти тяжёлые запросы и убрать лишние",
    "Добавить тёмную тему в панель настроек",
    "Написать документацию по запуску проекта для новых разработчиков",
    "Разобраться, почему периодически падает тест оформления заказа",
    "Вынести работу с почтой в отдельный сервис с очередью повторов",
    "Добавить пагинацию в таблицу клиентов и серверную сортировку",
    "Провести ревью последних изменений в модуле платежей",
    "Обновить зависимости и проверить, что сборка не сломалась",
    "Сделать форму регистрации доступной для чтения с экрана",
    "Добавить логирование ошибок интеграции с внешним API",
    "Спроектировать миграцию схемы базы данных для истории заказов",
    "Покрыть тестами сценарий восстановления пароля",
]


def _demo_agents(store: Any) -> list[dict[str, Any]]:
    return [a for a in store.list_agents() if a.get("demo")]


def seed_demo(store: Any) -> dict[str, Any]:
    """Create the demo team (idempotent). Returns {agents, edges} of the demo team."""
    by_name: dict[str, dict[str, Any]] = {}
    for name, role, model, color, x, y, _sup in _TEAM:
        existing = store.get_agent_by_name(name)
        if existing is not None:
            by_name[name] = existing
            continue
        by_name[name] = store.create_agent({
            "name": name, "runtime": "demo", "model": model, "role": role, "system_prompt": "",
            "cwd": None, "permissions": "edit", "color": color, "x": x, "y": y, "demo": True,
        })
    have = {(e["supervisor_id"], e["subordinate_id"]) for e in store.list_edges()}
    for name, *_rest, sup in _TEAM:
        if sup is None:
            continue
        pair = (by_name[sup]["id"], by_name[name]["id"])
        if pair not in have:
            store.create_edge(*pair)
    ids = {a["id"] for a in by_name.values()}
    edges = [e for e in store.list_edges() if e["supervisor_id"] in ids and e["subordinate_id"] in ids]
    return {"agents": [store.get_agent(a["id"]) for a in by_name.values()], "edges": edges}


async def clear_demo(store: Any, engine: Any) -> None:
    """Cancel active demo runs, then remove demo agents, edges and runs."""
    if engine is not None:
        for run in store.list_runs(status="running,queued", limit=100_000):
            if run.get("demo"):
                try:
                    await engine.cancel_run(run["id"])
                except Exception:
                    log.exception("cancelling demo run %s failed", run.get("id"))
    if hasattr(store, "delete_demo_data"):
        store.delete_demo_data()
        return
    for a in _demo_agents(store):
        store.delete_agent(a["id"])
    bus = getattr(engine, "bus", None)
    if bus is not None:
        bus.publish("demo.cleared", {})


class DemoDriver:
    """While `settings.demo_auto` is on, gives the demo lead a task every 20-40 s
    (never more than two demo root runs at once)."""

    def __init__(self, engine: Any, store: Any, *, interval_s: tuple[float, float] = (20.0, 40.0)) -> None:
        self.engine = engine
        self.store = store
        self.interval_s = interval_s
        self._task: asyncio.Task[None] | None = None
        self._last: str | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="orkestr-demo-driver")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(random.uniform(*self.interval_s))
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("demo driver tick failed")

    async def tick(self) -> dict[str, Any] | None:
        """One driver step; returns the started run or None."""
        if not self.store.get_settings().get("demo_auto"):
            return None
        active = [r for r in self.store.list_runs(status="running,queued", root_only=True, limit=1000)
                  if r.get("demo")]
        if len(active) >= 2:
            return None
        lead = self.store.get_agent_by_name(LEAD_NAME)
        if lead is None or not lead.get("demo"):
            return None
        choices = [t for t in DEMO_TASKS if t != self._last]
        task = random.choice(choices)
        self._last = task
        try:
            return await self.engine.start_run(lead["id"], task)
        except EngineError as e:
            log.info("demo driver: cannot start a run: %s", e)
            return None
