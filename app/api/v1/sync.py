"""仅本机可访问的同步管理接口。"""
from fastapi import APIRouter, Request, Body, Query

from ..access import require_sync_manager
from ...errors import ApiError

router = APIRouter(prefix="/sync", tags=["数据同步"])


@router.get("/status")
def status(request: Request):
    return require_sync_manager(request).status()


@router.get("/settings")
def settings(request: Request):
    return require_sync_manager(request).settings_view()


@router.patch("/settings")
def settings_update(request: Request, body: dict = Body(...)):
    return require_sync_manager(request).settings_update(body)


@router.post("/test-connection")
def connection(request: Request):
    return require_sync_manager(request).test_connection()


@router.post("/initialize")
def initialize(request: Request, body: dict = Body(...)):
    if body.get("confirm") is not True:
        raise ApiError("CONFIRM_REQUIRED", "请确认初始化来源", status=409,
                       user_message="请先确认使用哪一侧的数据",
                       user_hint="确认后方可执行，避免覆盖现有数据")
    return require_sync_manager(request).initialize(body.get("source"))


@router.post("/run")
def run(request: Request, body: dict = Body(default={})):
    return require_sync_manager(request).run(synchronize=body.get("synchronize", False) is True)


@router.post("/offline")
def offline(request: Request):
    return require_sync_manager(request).offline()


@router.get("/conflicts/{identity}")
def conflicts(request: Request, identity: str, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100), unresolved: bool = False):
    return require_sync_manager(request).conflict_page(identity, offset, limit, unresolved)


@router.patch("/conflicts/{identity}")
def resolve(request: Request, identity: str, body: dict = Body(...)):
    return require_sync_manager(request).resolve(identity, body.get("revision"), body.get("choices", {}))


@router.post("/conflicts/{identity}/validate")
def validate(request: Request, identity: str, body: dict = Body(...)):
    return require_sync_manager(request).validate_resolution(identity, body.get("revision"))


@router.post("/conflicts/{identity}/apply")
def apply(request: Request, identity: str, body: dict = Body(...)):
    return require_sync_manager(request).apply_resolution(identity, body.get("revision"), body.get("confirm") is True)


@router.post("/conflicts/{identity}/cancel")
def cancel(request: Request, identity: str):
    return require_sync_manager(request).cancel(identity)

@router.get("/conflicts/{identity}/details")
def details(request: Request, identity: str, conflict_id: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return require_sync_manager(request).conflict_details(identity, conflict_id, offset, limit)


@router.get("/conflicts/{identity}/changes")
def changes(request: Request, identity: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return require_sync_manager(request).changes_page(identity, offset, limit)


# ---------- 本地备份历史 ----------

@router.get("/backups")
def backups(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
    return require_sync_manager(request).backup_page(offset, limit)


@router.post("/backups")
def create_backup(request: Request, body: dict = Body(default={})):
    if body.get("dry_run") is not True:
        key = body.get("idempotency_key")
        if not isinstance(key, str) or not key.strip():
            raise ApiError("BACKUP_IDEMPOTENCY_REQUIRED", "正式备份必须提供 idempotency_key",
                           status=409, user_message="操作标识缺失",
                           user_hint="请刷新页面后重试")
    return require_sync_manager(request).local_backup(
        dry_run=body.get("dry_run") is True, idempotency_key=body.get("idempotency_key"))


@router.post("/backups/{backup_id}/restore")
def restore_backup(request: Request, backup_id: str, body: dict = Body(default={})):
    if body.get("dry_run") is not True:
        key = body.get("idempotency_key")
        if body.get("confirm") is not True or not isinstance(key, str) or not key.strip():
            raise ApiError("RESTORE_CONFIRM_REQUIRED", "还原必须明确确认并提供 idempotency_key",
                           status=409, user_message="还原前需要确认",
                           user_hint="请确认还原后重试")
    return require_sync_manager(request).restore_backup(
        backup_id, dry_run=body.get("dry_run") is True,
        confirm=body.get("confirm") is True, idempotency_key=body.get("idempotency_key"))
