"""HTTP-клієнт веб-інтерфейсу до API Gateway.

Web UI — окремий мікросервіс без доступу до БД: усі дані він отримує з API Gateway за REST,
передаючи токен користувача (JWT) у заголовку Authorization. Відповідь із помилкою стає
винятком ApiError з текстом, який можна показати користувачу; прострочений токен (401)
завершує сеанс і повертає на сторінку входу.

    rows = await api.get("/api/incidents", {"status": "open"})
    await api.post(f"/api/incidents/{inc_id}/acknowledge")
"""
import logging
from email.message import Message

import httpx
from nicegui import app, ui

from ..settings import settings

_client: httpx.AsyncClient | None = None
logging.getLogger("httpx").setLevel(logging.WARNING)     # без запису про кожен запит до API Gateway


class ApiError(Exception):
    def __init__(self, status: int, message: str, payload: dict | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.payload = payload or {}


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=settings.gateway_url, timeout=60)
    return _client


async def close():
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _detail(payload: dict, fallback: str) -> str:
    detail = payload.get("detail")
    if isinstance(detail, list):        # помилки перевірки запиту (FastAPI / pydantic)
        detail = "; ".join(f"{'.'.join(str(x) for x in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail)
    return str(detail or fallback)


async def request(method: str, path: str, *, params: dict | None = None, json=None, content: bytes | None = None,
                  headers: dict | None = None, auth: bool = True) -> httpx.Response:
    h = dict(headers or {})
    if auth and app.storage.user.get("token"):
        h["Authorization"] = f"Bearer {app.storage.user['token']}"
    params = {k: v for k, v in (params or {}).items() if v is not None}
    try:
        r = await client().request(method, path, params=params, json=json, content=content, headers=h)
    except httpx.HTTPError as e:
        raise ApiError(503, f"API Gateway недоступний ({e.__class__.__name__})") from None
    if r.status_code == 401 and auth:
        app.storage.user.clear()
        ui.navigate.to("/login")
        raise ApiError(401, "Сеанс завершено — увійдіть знову")
    if r.status_code >= 400:
        try:
            payload = r.json()
        except ValueError:
            payload = {"detail": r.text}
        raise ApiError(r.status_code, _detail(payload if isinstance(payload, dict) else {}, r.reason_phrase), payload)
    return r


def _json(r: httpx.Response):
    return r.json() if r.content else None


async def get(path: str, params: dict | None = None):
    return _json(await request("GET", path, params=params))


async def post(path: str, json=None, params: dict | None = None, auth: bool = True):
    return _json(await request("POST", path, json=json, params=params, auth=auth))


async def put(path: str, json=None, params: dict | None = None):
    return _json(await request("PUT", path, json=json, params=params))


async def patch(path: str, json=None, params: dict | None = None):
    return _json(await request("PATCH", path, json=json, params=params))


async def delete(path: str, params: dict | None = None):
    return _json(await request("DELETE", path, params=params))


async def upload(path: str, content: bytes, filename: str, params: dict | None = None):
    """Тіло запиту — сам файл; ім'я файлу — у заголовку X-Filename."""
    return _json(await request("POST", path, content=content, params=params,
                               headers={"Content-Type": "application/octet-stream", "X-Filename": filename}))


async def download(path: str, params: dict | None = None) -> tuple[bytes, str, str]:
    """Файл із відповіді: (вміст, ім'я файлу, тип вмісту)."""
    r = await request("GET", path, params=params)
    msg = Message()
    msg["content-disposition"] = r.headers.get("content-disposition", "")
    return r.content, msg.get_param("filename", "download", header="content-disposition"), \
        r.headers.get("content-type", "application/octet-stream")
