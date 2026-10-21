"""Звіти (FR-10): API Gateway маршрутизує запит до Reporting Service, передаючи токен користувача."""
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from ...auth import Principal
from .. import deps

router = APIRouter(prefix="/api/reports", tags=["reports"])


async def forward(request: Request, path: str) -> httpx.Response:
    try:
        return await deps.reporting.get(path, params=request.query_params,
                                        headers={"Authorization": request.headers["authorization"]})
    except httpx.HTTPError as e:
        raise HTTPException(503, f"Reporting Service недоступний: {e.__class__.__name__}") from None


@router.get("")
async def reports(request: Request, _user: Principal = Depends(deps.current_user)):
    r = await forward(request, "/reports")
    return JSONResponse(r.json(), status_code=r.status_code)


@router.get("/export")
async def export(request: Request, _user: Principal = Depends(deps.current_user)):
    r = await forward(request, "/reports/export")
    headers = {k: v for k, v in r.headers.items() if k.lower() == "content-disposition"}
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"), headers=headers)
