"""Работа с билетной системой QTickets по её REST API.

Документация: https://qtickets.help/article/rest-api/
Все запросы идут на https://qtickets.ru/api/rest/v1/{метод}
с заголовком Authorization: Bearer ТОКЕН.

Особенность QTickets: фильтры и номер страницы передаются в ТЕЛЕ запроса,
даже когда сам запрос — GET. Это необычно, поэтому на всякий случай бот
дополнительно перепроверяет каждую запись на своей стороне: если сервер
вдруг проигнорирует фильтр, подсчёт всё равно останется верным.
"""

from __future__ import annotations  # чтобы код работал и на Python 3.9

import asyncio
import logging
from datetime import datetime

import aiohttp

log = logging.getLogger(__name__)


class QTicketsError(Exception):
    """Понятная человеку ошибка при обращении к QTickets."""


def _parse_dt(value) -> datetime | None:
    """Разбор даты вида 2026-08-05T19:00:00+05:00 (так их отдаёт QTickets)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _event_name(value, event_id: int) -> str:
    """Название мероприятия ровно в том виде, как оно записано в QTickets.

    Единственная обработка — обрезка лишних пробелов по краям. Если поле
    названия придёт разложенным по языкам (такое встречается у мультиязычных
    кабинетов), берём русский вариант, иначе первый непустой.
    """
    if isinstance(value, dict):
        for key in ("ru", "ru_RU", "default"):
            if value.get(key):
                value = value[key]
                break
        else:
            value = next((v for v in value.values() if v), None)
    text = str(value).strip() if value is not None else ""
    return text or f"Мероприятие {event_id}"


class QTicketsClient:
    def __init__(self, token: str, base_url: str, timeout_sec: int = 25):
        self._token = token
        self._base = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout_sec)

    async def _request(self, method: str, path: str, payload: dict | None = None):
        # Предохранитель: бот только ЧИТАЕТ данные QTickets. Любой другой вид
        # запроса — ошибка в коде, а не рабочая ситуация. Особенно важно для
        # адреса со штрихкодами: запрос на запись по тому же адресу отмечает
        # билеты как отсканированные на входе.
        if method.upper() != "GET":
            raise QTicketsError(f"Бот не должен изменять данные QTickets ({method} {path}).")
        if "barcode" in path and payload is not None:
            raise QTicketsError("Запрос штрихкодов отправляется без тела.")
        url = f"{self._base}/{path.lstrip('/')}"
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        last_error: QTicketsError | None = None
        for attempt in range(3):
            try:
                async with aiohttp.ClientSession(timeout=self._timeout) as session:
                    async with session.request(
                        method, url, headers=headers, json=payload
                    ) as resp:
                        if resp.status == 200:
                            return await resp.json(content_type=None)
                        body = (await resp.text())[:300]
                        if resp.status == 401:
                            raise QTicketsError(
                                "QTickets не принял токен (ошибка 401). "
                                "Проверьте QTICKETS_TOKEN в файле .env."
                            )
                        if resp.status == 403:
                            raise QTicketsError(
                                "QTickets отказал в доступе (ошибка 403). "
                                "Проверьте токен и его права в личном кабинете."
                            )
                        if resp.status == 404:
                            raise QTicketsError(
                                "QTickets: такой записи нет (ошибка 404) — проверьте ID."
                            )
                        if resp.status == 429 or 500 <= resp.status < 600:
                            last_error = QTicketsError(
                                f"QTickets временно недоступен или перегружен "
                                f"(ошибка {resp.status})."
                            )
                            await asyncio.sleep(2 * (attempt + 1))
                            continue
                        raise QTicketsError(
                            f"QTickets вернул ошибку {resp.status}: {body}"
                        )
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_error = QTicketsError(
                    f"Нет связи с QTickets ({e.__class__.__name__}). Попробуйте позже."
                )
                await asyncio.sleep(2 * (attempt + 1))
        raise last_error or QTicketsError("Нет связи с QTickets.")

    @staticmethod
    def _data_list(resp) -> tuple[list, dict]:
        """Достать из ответа список записей и сведения о страницах."""
        if isinstance(resp, dict):
            data = resp.get("data")
            if isinstance(data, list):
                return data, resp.get("paging") or {}
        if isinstance(resp, list):
            return resp, {}
        return [], {}

    # ---------- Мероприятия ----------

    async def list_events(self) -> list[dict]:
        """Список неудалённых мероприятий (последние — первыми)."""
        payload = {
            "where": [{"column": "deleted_at", "operator": "null"}],
            "orderBy": {"id": "desc"},
            "page": 1,
        }
        resp = await self._request("GET", "events", payload)
        items, _ = self._data_list(resp)
        out = []
        for it in items:
            if isinstance(it, dict) and it.get("id") is not None:
                try:
                    ev_id = int(it["id"])
                except (TypeError, ValueError):
                    continue
                out.append(
                    {"id": ev_id, "name": _event_name(it.get("name"), ev_id)}
                )
        return out

    async def get_event(self, event_id: int) -> dict:
        """Название мероприятия и список его сеансов (id + дата-время начала)."""
        resp = await self._request("GET", f"events/{int(event_id)}")
        data = resp.get("data") if isinstance(resp, dict) else None
        if not isinstance(data, dict) or data.get("id") is None:
            raise QTicketsError(f"QTickets не вернул данные мероприятия {event_id}.")
        shows = []
        for s in data.get("shows") or []:
            if not isinstance(s, dict):
                continue
            start = _parse_dt(s.get("start_date"))
            if s.get("id") is None or start is None:
                continue
            if s.get("deleted_at"):
                continue  # удалённые сеансы не считаем никогда
            # Выключенный сеанс НЕ отбрасываем: в QTickets «выключен» часто
            # значит лишь «продажи закрыты», а зрители с билетами (например,
            # по промокодам) на него всё равно придут. Что показывать в чате,
            # решает отчёт: выключенный сеанс без билетов там не упоминается.
            active = s.get("is_active") not in (0, False, "0")
            shows.append({"id": int(s["id"]), "start": start, "active": active})
        return {
            "id": int(data["id"]),
            "name": _event_name(data.get("name"), event_id),
            "shows": shows,
        }

    # ---------- Подсчёт билетов ----------

    async def count_show_tickets(self, show_ids: set[int]) -> dict[int, int]:
        """Сколько действующих билетов на каждом сеансе — как в кабинете QTickets.

        Для каждого сеанса берём список его действующих штрихкодов
        (GET /shows/{id}/barcodes). Один билет — один штрихкод, отменённые
        и возвращённые туда не входят. Так бот видит все билеты сеанса,
        как бы их ни оформили: оплатой, подарочным сертификатом, промокодом
        на полную сумму, приглашением из кабинета.
        """
        counts: dict[int, int] = {}
        for sid in sorted(int(x) for x in show_ids):
            resp = await self._request("GET", f"shows/{sid}/barcodes")
            paging = {}
            if isinstance(resp, dict) and isinstance(resp.get("data"), list):
                items = resp["data"]
                paging = resp.get("paging") or {}
            elif isinstance(resp, list):
                items = resp
            else:
                raise QTicketsError(f"Непонятный ответ QTickets по билетам сеанса {sid}.")
            seen = set()
            for it in items:
                if not isinstance(it, dict):
                    continue
                key = it.get("id")
                if key is None:
                    key = it.get("barcode")
                if key is None or key in seen:
                    continue
                seen.add(key)
            n = len(seen)
            # Если QTickets когда-нибудь начнёт отдавать билеты по страницам,
            # в ответе будет общее число — берём его, а не длину первой страницы.
            try:
                total = int(paging.get("total") or 0)
            except (TypeError, ValueError):
                total = 0
            counts[sid] = max(n, total)
        return counts

    async def count_tickets(
        self, event_id: int, show_ids: set[int]
    ) -> tuple[dict[int, int], bool]:
        """Главный подсчёт для отчёта. Возвращает (числа по сеансам, запасной_способ).

        Основной способ — по билетам сеанса (как в кабинете). Если QTickets
        по какой-то причине не отдал их, считаем прежним способом — по
        оплаченным заказам. Отчёт всё равно уйдёт, но с пометкой: билеты по
        промокодам при этом могли не попасть в счёт, и молчать об этом нельзя.
        """
        if not show_ids:
            return {}, False
        try:
            return await self.count_show_tickets(show_ids), False
        except QTicketsError as e:
            log.warning(
                "Не удалось получить билеты по сеансам (%s) — считаю по заказам.", e
            )
            return await self.count_paid_tickets(event_id, show_ids), True

    async def count_paid_tickets(
        self, event_id: int, show_ids: set[int]
    ) -> dict[int, int]:
        """Запасной способ: оплаченные билеты по списку заказов мероприятия.

        Используется, только если QTickets не отдал билеты по сеансам.

        Идём по списку заказов мероприятия (только оплаченные), внутри каждого
        заказа смотрим билеты: пропускаем удалённые и возвращённые, остальные
        суммируем по сеансам.
        """
        counts = {int(sid): 0 for sid in show_ids}
        if not counts:
            return counts

        page = 1
        warned_no_baskets = False
        while page <= 50:  # предохранитель от бесконечного цикла
            payload = {
                "where": [
                    {"column": "event_id", "value": int(event_id)},
                    {"column": "payed", "value": 1},
                ],
                "orderBy": {"id": "asc"},
                "page": page,
            }
            resp = await self._request("GET", "orders", payload)
            items, paging = self._data_list(resp)
            if not items:
                break

            for order in items:
                if not isinstance(order, dict):
                    continue
                # Перепроверка на нашей стороне (см. пояснение в начале файла)
                try:
                    if int(order.get("event_id") or 0) != int(event_id):
                        continue
                except (TypeError, ValueError):
                    continue
                if not order.get("payed"):
                    continue
                if order.get("deleted_at"):
                    continue

                baskets = order.get("baskets")
                if baskets is None:
                    # Если список заказов пришёл без состава — дотягиваем заказ отдельно
                    if not warned_no_baskets:
                        log.warning(
                            "QTickets: список заказов без состава билетов — "
                            "запрашиваю заказы по одному."
                        )
                        warned_no_baskets = True
                    detail = await self._request("GET", f"orders/{order.get('id')}")
                    dd = detail.get("data") if isinstance(detail, dict) else None
                    baskets = (dd or {}).get("baskets") or []

                for b in baskets or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("deleted_at") or b.get("refunded_at"):
                        continue
                    sid = b.get("show_id")
                    if sid is None:
                        continue
                    try:
                        sid = int(sid)
                    except (TypeError, ValueError):
                        continue
                    if sid not in counts:
                        continue
                    try:
                        qty = int(b.get("quantity") or 1)
                    except (TypeError, ValueError):
                        qty = 1
                    counts[sid] += max(qty, 1)

            per_page = int(paging.get("perPage") or 100) if paging else 100
            total = int(paging.get("total") or 0) if paging else 0
            if total and page * per_page >= total:
                break
            if not paging and len(items) < per_page:
                break
            page += 1

        return counts


_client: QTicketsClient | None = None


def get_client() -> QTicketsClient:
    global _client
    if _client is None:
        import config

        _client = QTicketsClient(config.QTICKETS_TOKEN, config.QTICKETS_BASE_URL)
    return _client
