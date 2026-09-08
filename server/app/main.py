from __future__ import annotations

import logging
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import models as m
from .auth import (
    ROLE_ADMIN,
    ROLE_USER,
    AppUser,
    CurrentUser,
    audit_actor,
    create_auth_router,
    initial_credentials_directory,
    require_roles,
    seed_default_users_from_env,
    write_generated_credentials,
)
from .config import Settings
from .database import Database, get_db
from .domain import ATTITUDE_LABELS
from .model_adapters import (
    ModelAdapterError,
    OllamaIntentModelAdapter,
    get_model_adapter,
)
from .model_settings import ModelSettingsError, ModelSettingsStore
from .schema_migrations import migrate_database
from .stage_adapters import get_stage_adapter
from .presenters import (
    dashboard_matrix,
    dashboard_summary,
    frontend_compat_dataset,
    iso,
    public_evidence,
    public_formal_status,
    public_master_data,
    public_material,
    public_observation,
    public_package,
    public_speaker,
    public_task,
)
from .schemas import (
    AnalyzeRequest,
    EvalRunRequest,
    IntentConfirmRequest,
    MeetingPackageCreate,
    MeetingPackageUpdate,
    ModelConnectionRequest,
    ModelConnectionTestResponse,
    ModelListResponse,
    ModelSettingsResponse,
    ModelSettingsUpdateRequest,
    ObservationReviewDraftRequest,
    ObservationUpdateRequest,
    PlatformDraftRequest,
    SpeakerConfirmRequest,
    SpeakerMergeRequest,
    SpeakerSplitRequest,
    TaxonomyImportCommitRequest,
)
from .seed import seed_demo_data
from .services import (
    DomainError,
    cancel_active_analysis_runs,
    confirm_observation,
    confirm_speaker,
    create_analysis_run,
    ensure_ai_snapshot,
    merge_speakers,
    observation_review_snapshot,
    request_analysis_run_cancellation,
    save_uploaded_file,
    split_speaker,
)
from .stage_orchestrator import (
    AnalysisQueue,
    mark_stale_analysis_runs_failed,
)
from .exporting import export_package
from .evaluation import run_evaluation
from .query_visibility import (
    get_visible_observation,
    get_visible_speaker,
    visible_observation_clause,
    visible_speaker_clause,
)
from .media_processing import ffmpeg_available, ffprobe_available
from .taxonomy_excel import (
    DEFAULT_MAX_XLSX_BYTES,
    preflight_taxonomy_xlsx,
)
from .taxonomy_service import (
    TaxonomyServiceError,
    catalogue,
    commit_import,
    import_diff,
    propose_observation_platform,
)


DbSession = Annotated[Session, Depends(get_db)]
EditorUser = Annotated[
    AppUser, Depends(require_roles(ROLE_ADMIN, ROLE_USER))
]
AdminUser = Annotated[AppUser, Depends(require_roles(ROLE_ADMIN))]
logger = logging.getLogger(__name__)


def _get_or_404(session: Session, model, item_id: str, label: str):
    item = session.get(model, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"{label}不存在")
    return item


def _get_visible_observation_or_404(
    session: Session, observation_id: str
) -> m.RawIntentObservation:
    item = get_visible_observation(session, observation_id)
    if item is None:
        raise HTTPException(status_code=404, detail="原始意向观察不存在")
    return item


def _get_visible_speaker_or_404(
    session: Session, speaker_id: str
) -> m.SpeakerCluster:
    item = get_visible_speaker(session, speaker_id)
    if item is None:
        raise HTTPException(status_code=404, detail="说话人不存在")
    return item


def _platform_taxonomy_response(session: Session) -> dict:
    """Adapt the backend catalogue to the one shared frontend cascade shape."""

    raw = catalogue(session)
    revision = session.scalar(
        select(func.count()).select_from(m.TaxonomyImportBatch)
    ) or 0
    technologies: list[dict] = []
    for technology in raw["technologies"]:
        levels = []
        for key in ("level2", "level3"):
            level = technology.get(key)
            if level is None:
                continue
            levels.append(
                {
                    "id": level["id"],
                    "level": level["level"],
                    "name": level["name"],
                    "values": [
                        {
                            "id": value["id"],
                            "name": value["value"],
                            "status": value["status"],
                            "sortOrder": value["sortOrder"],
                        }
                        for value in level["values"]
                    ],
                }
            )
        technologies.append(
            {
                "id": technology["id"],
                "name": technology["name"],
                "code": technology["code"],
                "status": "active",
                "levels": levels,
                "leaves": [
                    {
                        "id": item["id"],
                        "genericPlatformId": item["genericPlatformId"],
                        "displayName": item["name"],
                        "level2ValueId": item["level2ValueId"],
                        "level3ValueId": item["level3ValueId"],
                        "status": "active",
                        "mappingStatus": item["mappingStatus"],
                    }
                    for item in technology["platforms"]
                ],
            }
        )
    return {
        "revision": revision,
        "technologies": technologies,
        "legacyPlatforms": raw["legacyPlatforms"],
    }


def create_app(
    *,
    database_url: str | None = None,
    data_dir: str | Path | None = None,
    seed: bool = True,
) -> FastAPI:
    settings = Settings.load(database_url=database_url, data_dir=data_dir)
    model_settings = ModelSettingsStore(settings.data_dir)
    migrate_database(settings.server_dir, settings.database_url)
    database = Database(settings.database_url)
    database.create_all()
    with database.session() as session:
        if seed:
            seed_demo_data(session, settings)
        seeded_users = seed_default_users_from_env(session, commit=False)
        try:
            generated_credentials_dir = write_generated_credentials(
                settings.data_dir,
                seeded_users,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
    if generated_credentials_dir is not None:
        logger.warning(
            "已生成本地初始登录凭据。请仅在本机打开：%s",
            generated_credentials_dir,
        )

    analysis_queue = AnalysisQueue(database, model_settings, settings)

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        stale_count = mark_stale_analysis_runs_failed(database)
        if stale_count:
            logger.warning("已将 %s 个中断的分析任务标记为处理失败", stale_count)
        analysis_queue.start()
        try:
            yield
        finally:
            analysis_queue.stop()

    app = FastAPI(
        title="意向宝智能体 API",
        summary="把客户会议材料整理成可追溯、可确认的产品规划意向地图",
        description=(
            "首版为本地多角色权限模式，默认使用清晰标识的模拟数据和 mock 分析器。"
            "真实模型接口已预留，但未启用，也未宣称达到85%准确率。"
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.model_settings = model_settings
    app.state.database = database
    app.state.analysis_queue = analysis_queue
    app.state.initial_credentials_directory = initial_credentials_directory(
        settings.data_dir
    )
    app.state.taxonomy_previews = {}
    app.include_router(create_auth_router())

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(DomainError)
    async def domain_error_handler(
        _request: Request, exc: DomainError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message},
        )

    @app.exception_handler(TaxonomyServiceError)
    async def taxonomy_error_handler(
        _request: Request, exc: TaxonomyServiceError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"detail": exc.message, "code": exc.code},
        )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # FastAPI's default validation body includes the rejected input. That
        # is unsafe for write-only fields such as api_key, so only expose the
        # location and human-readable validation message.
        safe_errors = [
            {
                "type": item.get("type"),
                "loc": item.get("loc"),
                "msg": item.get("msg"),
            }
            for item in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": safe_errors})

    @app.get("/api/health", tags=["系统"])
    def health(session: DbSession) -> dict:
        session.execute(select(1))
        active_model = model_settings.snapshot()
        return {
            "status": "ok",
            "service": "intent-bao-agent-api",
            "version": app.version,
            "model_adapter": active_model.provider,
            "model_name": active_model.model_name,
            "data_scope": "本地多角色权限",
            "time": datetime.now(timezone.utc).isoformat(),
        }

    def adapter_from_probe(payload: ModelConnectionRequest):
        current = model_settings.snapshot()
        provider = payload.provider or current.provider
        base_url = payload.base_url or current.base_url
        model_name = payload.model_name or current.model_name
        api_key = (
            payload.api_key.get_secret_value()
            if payload.api_key is not None
            else current.api_key
        )
        try:
            return get_model_adapter(
                provider,
                base_url=base_url,
                model_name=model_name,
                api_key=api_key,
            )
        except (ModelSettingsError, ValueError) as exc:
            raise DomainError(422, str(exc)) from exc

    @app.get(
        "/api/model-settings",
        tags=["模型设置"],
        response_model=ModelSettingsResponse,
    )
    def get_model_settings(_user: AdminUser) -> dict:
        return model_settings.public_dict()

    @app.put(
        "/api/model-settings",
        tags=["模型设置"],
        response_model=ModelSettingsResponse,
    )
    def update_model_settings(
        payload: ModelSettingsUpdateRequest, _user: AdminUser
    ) -> dict:
        try:
            updated = model_settings.update(
                provider=payload.provider,
                base_url=payload.base_url,
                model_name=payload.model_name,
                api_key=(
                    payload.api_key.get_secret_value()
                    if payload.api_key is not None
                    else None
                ),
                clear_api_key=payload.clear_api_key,
                asr_provider=payload.asr_provider,
                asr_base_url=payload.asr_base_url,
                asr_model_name=payload.asr_model_name,
            )
        except ModelSettingsError as exc:
            raise DomainError(422, str(exc)) from exc
        return updated.public_dict()

    @app.delete(
        "/api/model-settings/api-key",
        tags=["模型设置"],
        response_model=ModelSettingsResponse,
    )
    def clear_model_api_key(_user: AdminUser) -> dict:
        return model_settings.clear_api_key().public_dict()

    @app.post(
        "/api/model-settings/test",
        tags=["模型设置"],
        response_model=ModelConnectionTestResponse,
    )
    def test_model_connection(
        payload: ModelConnectionRequest,
        _user: AdminUser,
    ) -> dict:
        adapter = adapter_from_probe(payload)
        if adapter.name == "mock":
            return {
                "ok": True,
                "message": "模拟分析器可用；它仅用于验证业务流程，不代表真实模型能力。",
                "models": [],
                "latency_ms": 0,
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
        if not isinstance(adapter, OllamaIntentModelAdapter):
            raise DomainError(422, "当前模型提供方不支持连接测试")
        return adapter.test_connection()

    @app.post(
        "/api/model-settings/models",
        tags=["模型设置"],
        response_model=ModelListResponse,
    )
    def list_model_options(
        payload: ModelConnectionRequest, _user: AdminUser
    ) -> dict:
        adapter = adapter_from_probe(payload)
        if adapter.name == "mock":
            return {
                "models": [],
                "checked_at": datetime.now(timezone.utc).isoformat(),
            }
        if not isinstance(adapter, OllamaIntentModelAdapter):
            raise DomainError(422, "当前模型提供方不支持读取模型列表")
        try:
            models = adapter.list_models()
        except ModelAdapterError as exc:
            raise DomainError(502, str(exc)) from exc
        return {
            "models": models,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/bootstrap", tags=["系统"])
    def bootstrap(session: DbSession, _user: CurrentUser) -> dict:
        packages = session.scalars(
            select(m.MeetingPackage)
            .order_by(m.MeetingPackage.meeting_date.desc())
            .limit(20)
        ).all()
        compatibility = frontend_compat_dataset(session)
        return {
            "project": {
                "id": "project-mnt",
                "name": "MNT客户意向地图试点",
                "mode": "本地多角色权限",
                "data_scope": "模拟数据",
            },
            "dashboard": dashboard_summary(session),
            "master_data": public_master_data(session),
            "meeting_packages": {
                "items": [public_package(session, item) for item in packages],
                "total": session.scalar(
                    select(func.count()).select_from(m.MeetingPackage)
                )
                or 0,
            },
            **compatibility,
        }

    @app.get("/api/master-data", tags=["基础字典"])
    def master_data(session: DbSession, _user: CurrentUser) -> dict:
        data = public_master_data(session)
        return {
            **data,
            "data_scope": "模拟数据",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/platform-taxonomy", tags=["数据字典"])
    def platform_taxonomy(
        session: DbSession, _user: CurrentUser
    ) -> dict:
        return _platform_taxonomy_response(session)

    @app.post("/api/taxonomy/import/preflight", tags=["数据字典"])
    async def preflight_taxonomy_import(
        request: Request,
        session: DbSession,
        _admin: AdminUser,
        file: Annotated[UploadFile, File(...)],
    ) -> dict:
        content = await file.read(DEFAULT_MAX_XLSX_BYTES + 1)
        result = preflight_taxonomy_xlsx(
            content,
            filename=file.filename or "upload.xlsx",
        )
        response = dict(result)
        response["diff"] = None
        if not result["valid"] or result["preview"] is None:
            return response

        try:
            diff = import_diff(session, result["preview"])
        except TaxonomyServiceError as exc:
            raise DomainError(422, f"{exc.code}：{exc.message}") from exc

        now = time.monotonic()
        previews: dict = request.app.state.taxonomy_previews
        for key, item in list(previews.items()):
            if now - item["created_at"] > 30 * 60:
                previews.pop(key, None)
        token = secrets.token_urlsafe(32)
        previews[token] = {
            "created_at": now,
            "file": dict(result["file"]),
            "preview": result["preview"],
            "diff": diff,
        }
        return {
            **response,
            "diff": diff,
            "preview_token": token,
        }

    @app.post("/api/taxonomy/import/commit", tags=["数据字典"])
    def commit_taxonomy_import(
        payload: TaxonomyImportCommitRequest,
        request: Request,
        session: DbSession,
        admin: AdminUser,
    ) -> dict:
        preview = request.app.state.taxonomy_previews.get(
            payload.preview_token
        )
        if preview is None or time.monotonic() - preview["created_at"] > 30 * 60:
            raise DomainError(410, "Excel预检已过期，请重新上传预检")
        if preview["file"]["sha256"] != payload.file_sha256:
            raise DomainError(409, "提交文件与预检文件不一致")
        try:
            result = commit_import(
                session,
                preview["preview"],
                file_name=preview["file"]["name"],
                file_sha256=preview["file"]["sha256"],
                actor=audit_actor(admin),
            )
        except TaxonomyServiceError as exc:
            raise DomainError(422, f"{exc.code}：{exc.message}") from exc
        return {
            "message": (
                "相同版本已发布，无需重复写入。"
                if result["idempotent"]
                else "数据字典已按安全合并方式发布。"
            ),
            "idempotent": result["idempotent"],
            "result": result,
            "taxonomy": _platform_taxonomy_response(session),
        }

    @app.get("/api/meeting-packages", tags=["会议资料包"])
    def list_meeting_packages(
        session: DbSession,
        _user: CurrentUser,
        status: str | None = None,
        customer_id: str | None = None,
        q: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict:
        query = select(m.MeetingPackage)
        count_query = select(func.count()).select_from(m.MeetingPackage)
        conditions = []
        if status:
            conditions.append(m.MeetingPackage.status == status)
        if customer_id:
            conditions.append(m.MeetingPackage.customer_id == customer_id)
        if q:
            conditions.append(
                or_(
                    m.MeetingPackage.title.contains(q),
                    m.MeetingPackage.notes.contains(q),
                )
            )
        if conditions:
            query = query.where(*conditions)
            count_query = count_query.where(*conditions)
        items = session.scalars(
            query.order_by(
                m.MeetingPackage.meeting_date.desc(),
                m.MeetingPackage.updated_at.desc(),
            )
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "items": [public_package(session, item) for item in items],
            "total": session.scalar(count_query) or 0,
            "offset": offset,
            "limit": limit,
        }

    @app.post("/api/meeting-packages", tags=["会议资料包"], status_code=201)
    def create_meeting_package(
        payload: MeetingPackageCreate,
        session: DbSession,
        _user: EditorUser,
    ) -> dict:
        _get_or_404(session, m.Project, payload.project_id, "项目")
        _get_or_404(session, m.Customer, payload.customer_id, "客户")
        for attendee in payload.attendees:
            if attendee.function_id:
                _get_or_404(
                    session, m.CustomerFunction, attendee.function_id, "客户职能"
                )
        item = m.MeetingPackage(
            project_id=payload.project_id,
            customer_id=payload.customer_id,
            title=payload.title,
            meeting_date=payload.meeting_date,
            data_scope=payload.data_scope,
            status="草稿",
            material_completeness="待检查",
            pdf_exception_reason=payload.pdf_exception_reason,
            retention_days=payload.retention_days,
            notes=payload.notes,
        )
        session.add(item)
        session.flush()
        for attendee in payload.attendees:
            session.add(
                m.MeetingAttendee(
                    package_id=item.id,
                    display_name=attendee.display_name,
                    role=attendee.role,
                    organization=attendee.organization,
                    function_id=attendee.function_id,
                    is_confirmed=attendee.function_id is not None,
                )
            )
        session.commit()
        session.refresh(item)
        return public_package(session, item, include_detail=True)

    @app.get("/api/meeting-packages/{package_id}", tags=["会议资料包"])
    def get_meeting_package(
        package_id: str, session: DbSession, _user: CurrentUser
    ) -> dict:
        item = _get_or_404(session, m.MeetingPackage, package_id, "会议资料包")
        return public_package(session, item, include_detail=True)

    @app.patch("/api/meeting-packages/{package_id}", tags=["会议资料包"])
    def update_meeting_package(
        package_id: str,
        payload: MeetingPackageUpdate,
        session: DbSession,
        _user: EditorUser,
    ) -> dict:
        item = _get_or_404(session, m.MeetingPackage, package_id, "会议资料包")
        if item.version != payload.version:
            raise HTTPException(
                status_code=409,
                detail=f"资料包已更新，请刷新后重试（当前版本{item.version}）",
            )
        changes = payload.model_dump(exclude_unset=True, exclude={"version"})
        for field, value in changes.items():
            setattr(item, field, value)
        item.version += 1
        session.commit()
        session.refresh(item)
        return public_package(session, item, include_detail=True)

    @app.post(
        "/api/meeting-packages/{package_id}/files",
        tags=["会议资料包"],
        status_code=201,
    )
    async def upload_material(
        package_id: str,
        session: DbSession,
        _user: EditorUser,
        file: Annotated[UploadFile, File(...)],
        material_type: Annotated[str | None, Form()] = None,
    ) -> dict:
        package = _get_or_404(
            session, m.MeetingPackage, package_id, "会议资料包"
        )
        item, duplicate = await save_uploaded_file(
            session, settings, package, file, material_type
        )
        return {
            "file": public_material(item),
            "duplicate_in_package": duplicate,
            "message": (
                "文件已保存；检测到相同内容，仍按新版本保留。"
                if duplicate
                else "文件已保存，等待预检。"
            ),
        }

    @app.post(
        "/api/meeting-packages/{package_id}/analyze",
        tags=["处理任务"],
        status_code=202,
    )
    def analyze_meeting_package(
        package_id: str,
        payload: AnalyzeRequest,
        session: DbSession,
        _user: EditorUser,
    ) -> dict:
        package = _get_or_404(
            session, m.MeetingPackage, package_id, "会议资料包"
        )
        active_model = model_settings.snapshot()
        requested_adapter = (payload.adapter or "").strip().lower() or None
        if requested_adapter not in {None, "mock", active_model.provider}:
            raise DomainError(
                422,
                "请先在“模型设置”中保存并启用所需模型，再启动分析。",
            )
        adapter_name = requested_adapter or active_model.provider
        adapter = None
        stage_adapter = None
        if adapter_name != "mock":
            try:
                adapter = get_model_adapter(
                    adapter_name,
                    base_url=active_model.base_url,
                    model_name=active_model.model_name,
                    api_key=active_model.api_key,
                )
                stage_adapter = get_stage_adapter(
                    active_model.provider,
                    base_url=active_model.base_url,
                    model_name=active_model.model_name,
                    api_key=active_model.api_key,
                )
            except (ModelSettingsError, ValueError) as exc:
                raise DomainError(422, str(exc)) from exc
        # A force re-run replaces whatever is queued/running so the new run
        # never waits behind a stale one in the serial worker.
        cancelled_runs = (
            cancel_active_analysis_runs(session, package_id)
            if payload.force
            else 0
        )
        run = create_analysis_run(
            session,
            package,
            adapter_name=adapter_name,
            force=payload.force,
            settings=settings,
            adapter=adapter,
            stage_adapter=stage_adapter,
            asr_settings=active_model.asr,
        )
        reused = (
            run.status == "已完成"
            or app.state.analysis_queue.contains(run.id)
        )
        if not reused:
            app.state.analysis_queue.enqueue(
                run.id,
                adapter=adapter,
                stage_adapter=stage_adapter,
                asr_settings=active_model.asr,
            )
        tasks = session.scalars(
            select(m.ProcessingTask)
            .where(m.ProcessingTask.analysis_run_id == run.id)
            .order_by(m.ProcessingTask.stage_order)
        ).all()
        return {
            "task_id": run.id,
            "cancelled_runs": cancelled_runs,
            "analysis_run": {
                "id": run.id,
                "package_id": run.package_id,
                "adapter": run.adapter,
                "model_name": run.model_name,
                "status": run.status,
                "result_summary": run.result_summary,
                "started_at": iso(run.started_at),
                "finished_at": iso(run.finished_at),
            },
            "stages": [public_task(item) for item in tasks],
            "message": (
                "已有分析任务正在后台处理中，请等待完成后刷新查看。"
                if reused and run.status == "运行中"
                else (
                    "检测到相同条件的历史已完成分析，直接复用该结果。"
                    if reused
                    else (
                        "分析任务已进入后台队列；可在“会议资料包”中查看"
                        "实时进度，完成后结果自动落库。"
                    )
                )
            ),
        }

    @app.post(
        "/api/meeting-packages/{package_id}/analyze/cancel",
        tags=["处理任务"],
    )
    def cancel_package_analysis_runs(
        package_id: str,
        session: DbSession,
        _user: EditorUser,
    ) -> dict:
        """Manually stop all queued/running analyses of one package."""
        package = _get_or_404(session, m.MeetingPackage, package_id, "会议资料包")
        cancelled = cancel_active_analysis_runs(session, package_id)
        return {
            "package_id": package.id,
            "cancelled_runs": cancelled,
            "message": (
                "已请求停止分析任务，正在运行的阶段将在数秒内退出。"
                if cancelled
                else "当前没有排队或运行中的分析任务。"
            ),
        }

    @app.get(
        "/api/meeting-packages/{package_id}/export",
        tags=["处理任务"],
    )
    def export_package_artifacts(
        package_id: str,
        session: DbSession,
        user: EditorUser,
        format: Annotated[str, Query()] = "csv",
    ) -> Response:
        package = _get_or_404(session, m.MeetingPackage, package_id, "会议资料包")
        try:
            filename, content = export_package(session, package_id, format)
        except ValueError as exc:
            raise DomainError(422, str(exc)) from exc
        session.add(
            m.ConfirmationRecord(
                target_type="meeting_package_export",
                target_id=package_id,
                action="导出",
                before_snapshot=None,
                after_snapshot={
                    "format": format.lower(),
                    "filename": filename,
                    "data_scope": package.data_scope,
                    "content_bytes": len(content),
                },
                actor=audit_actor(user),
                note="资料包范围导出",
            )
        )
        session.commit()
        media_type = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if filename.endswith(".xlsx")
            else "text/csv; charset=utf-8"
            if filename.endswith(".csv")
            else "text/markdown; charset=utf-8"
        )
        return Response(
            content=content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post(
        "/api/analysis-runs/{run_id}/cancel",
        tags=["处理任务"],
    )
    def cancel_analysis_run(
        run_id: str,
        session: DbSession,
        _user: EditorUser,
    ) -> dict:
        run = _get_or_404(session, m.AnalysisRun, run_id, "分析任务")
        cancellation_requested = request_analysis_run_cancellation(session, run)
        if not cancellation_requested:
            return {
                "analysis_run": {
                    "id": run.id,
                    "status": run.status,
                    "package_id": run.package_id,
                    "cancel_requested_at": iso(run.cancel_requested_at),
                },
                "message": "任务已结束，无需取消。",
            }
        return {
            "analysis_run": {
                "id": run.id,
                "status": run.status,
                "package_id": run.package_id,
                "cancel_requested_at": iso(run.cancel_requested_at),
            },
            "message": "任务已取消。",
        }

    @app.post(
        "/api/analysis-runs/{run_id}/retry",
        tags=["处理任务"],
    )
    def retry_analysis_run(
        run_id: str,
        session: DbSession,
        _user: EditorUser,
    ) -> dict:
        original = _get_or_404(session, m.AnalysisRun, run_id, "分析任务")
        package = _get_or_404(
            session, m.MeetingPackage, original.package_id, "会议资料包"
        )
        # Replace any queued/running run (including the retried one itself)
        # so the fresh run starts instead of queueing behind it.
        cancel_active_analysis_runs(session, package.id)
        active_model = model_settings.snapshot()
        adapter_name = active_model.provider
        adapter = None
        stage_adapter = None
        if adapter_name != "mock":
            try:
                adapter = get_model_adapter(
                    adapter_name,
                    base_url=active_model.base_url,
                    model_name=active_model.model_name,
                    api_key=active_model.api_key,
                )
                stage_adapter = get_stage_adapter(
                    active_model.provider,
                    base_url=active_model.base_url,
                    model_name=active_model.model_name,
                    api_key=active_model.api_key,
                )
            except (ModelSettingsError, ValueError) as exc:
                raise DomainError(422, str(exc)) from exc
        run = create_analysis_run(
            session,
            package,
            adapter_name=adapter_name,
            force=True,
            settings=settings,
            adapter=adapter,
            stage_adapter=stage_adapter,
            asr_settings=active_model.asr,
        )
        app.state.analysis_queue.enqueue(
            run.id,
            adapter=adapter,
            stage_adapter=stage_adapter,
            asr_settings=active_model.asr,
        )
        tasks = session.scalars(
            select(m.ProcessingTask)
            .where(m.ProcessingTask.analysis_run_id == run.id)
            .order_by(m.ProcessingTask.stage_order)
        ).all()
        return {
            "task_id": run.id,
            "analysis_run": {
                "id": run.id,
                "package_id": run.package_id,
                "adapter": run.adapter,
                "model_name": run.model_name,
                "status": run.status,
                "result_summary": run.result_summary,
            },
            "stages": [public_task(item) for item in tasks],
            "message": (
                f"已重新创建分析任务 {run.id} 并进入后台队列；"
                "完成后可通过任务接口查看进度。"
            ),
        }

    @app.get(
        "/api/analysis-runs/{run_id}/artifacts",
        tags=["处理任务"],
    )
    def analysis_run_artifacts(
        run_id: str,
        session: DbSession,
        _user: CurrentUser,
    ) -> dict:
        run = _get_or_404(session, m.AnalysisRun, run_id, "分析任务")
        transcripts = session.scalars(
            select(m.TranscriptSegment)
            .where(m.TranscriptSegment.analysis_run_id == run.id)
            .order_by(m.TranscriptSegment.order_index)
        ).all()
        alignments = session.scalars(
            select(m.AlignmentLink).where(
                m.AlignmentLink.analysis_run_id == run.id
            )
        ).all()
        files = session.scalars(
            select(m.MaterialFile).where(
                m.MaterialFile.package_id == run.package_id,
                m.MaterialFile.is_deleted.is_(False),
            )
        ).all()
        file_ids = [item.id for item in files]
        run_start = run.started_at
        run_end = run.finished_at or m.utcnow()
        pages = (
            session.scalars(
                select(m.PdfPageText).where(
                    m.PdfPageText.material_file_id.in_(file_ids),
                    m.PdfPageText.created_at >= run_start,
                    m.PdfPageText.created_at <= run_end,
                )
            ).all()
            if file_ids
            else []
        )
        frames = (
            session.scalars(
                select(m.KeyFrame).where(
                    m.KeyFrame.material_file_id.in_(file_ids),
                    m.KeyFrame.created_at >= run_start,
                    m.KeyFrame.created_at <= run_end,
                )
            ).all()
            if file_ids
            else []
        )
        return {
            "transcripts": [
                {
                    "id": item.id,
                    "start_ms": item.start_ms,
                    "end_ms": item.end_ms,
                    "text": item.text,
                    "speaker_label": item.speaker_label,
                    "confidence": item.confidence,
                }
                for item in transcripts
            ],
            "pages": [
                {
                    "id": item.id,
                    "page_no": item.page_no,
                    "text": item.text,
                    "char_count": item.char_count,
                }
                for item in pages
            ],
            "frames": [
                {
                    "id": item.id,
                    "time_ms": item.time_ms,
                    "image_relative_path": item.image_relative_path,
                    "ocr_text": item.ocr_text,
                }
                for item in frames
            ],
            "alignments": [
                {
                    "id": item.id,
                    "segment_id": item.segment_id,
                    "target_type": item.target_type,
                    "target_id": item.target_id,
                    "confidence": item.confidence,
                    "reason": item.reason,
                    "status": item.status,
                }
                for item in alignments
            ],
        }

    @app.patch(
        "/api/speakers/merge",
        tags=["说话人校验"],
    )
    def merge_speaker_endpoint(
        payload: SpeakerMergeRequest,
        session: DbSession,
        user: EditorUser,
    ) -> dict:
        _get_visible_speaker_or_404(session, payload.keep_speaker_id)
        for speaker_id in payload.merge_speaker_ids:
            _get_visible_speaker_or_404(session, speaker_id)
        speaker = merge_speakers(
            session,
            package_id=payload.package_id,
            keep_speaker_id=payload.keep_speaker_id,
            merge_speaker_ids=payload.merge_speaker_ids,
            actor=audit_actor(user),
        )
        return {
            "speaker": public_speaker(session, speaker),
            "message": "说话人已合并。",
        }

    @app.post(
        "/api/speakers/{speaker_id}/split",
        tags=["说话人校验"],
    )
    def split_speaker_endpoint(
        speaker_id: str,
        payload: SpeakerSplitRequest,
        session: DbSession,
        user: EditorUser,
    ) -> dict:
        _get_visible_speaker_or_404(session, speaker_id)
        speaker = split_speaker(
            session,
            package_id=payload.package_id,
            speaker_id=speaker_id,
            segment_ids=payload.segment_ids,
            actor=audit_actor(user),
        )
        return {
            "speaker": public_speaker(session, speaker),
            "message": "说话人已拆分。",
        }

    @app.get(
        "/api/env-check",
        tags=["系统"],
    )
    def environment_check(_user: AdminUser) -> dict:
        import shutil

        settings_snapshot = model_settings.snapshot()
        asr = settings_snapshot.asr
        asr_healthy = False
        if asr.enabled:
            try:
                response = httpx.get(
                    f"{asr.base_url}/health",
                    timeout=2.0,
                    trust_env=False,
                )
                asr_healthy = response.status_code == 200
            except Exception:
                asr_healthy = False
        return {
            "ffmpeg": ffmpeg_available(),
            "ffprobe": ffprobe_available(),
            "intent_provider": settings_snapshot.provider,
            "intent_model": settings_snapshot.model_name,
            "asr": asr.public_dict(),
            "asr_healthy": asr_healthy,
            "disk_free_gb": round(
                shutil.disk_usage(settings.data_dir).free / (1024**3), 1
            ),
            "ok": bool(
                ffmpeg_available()
                and ffprobe_available()
                or model_settings.snapshot().provider == "mock"
            ),
        }

    @app.post(
        "/api/eval/runs",
        tags=["评测"],
    )
    def create_eval_run(
        payload: EvalRunRequest,
        session: DbSession,
        _user: EditorUser,
    ) -> dict:
        _get_or_404(
            session, m.MeetingPackage, payload.package_id, "会议资料包"
        )
        report = run_evaluation(
            session,
            package_id=payload.package_id,
            analysis_run_id=payload.analysis_run_id,
            golden=[item.model_dump() for item in payload.golden],
        )
        return {"evaluation": report, "message": "评测完成。"}

    @app.get(
        "/api/meeting-packages/{package_id}/tasks", tags=["处理任务"]
    )
    def list_package_tasks(
        package_id: str, session: DbSession, _user: CurrentUser
    ) -> dict:
        _get_or_404(session, m.MeetingPackage, package_id, "会议资料包")
        items = session.scalars(
            select(m.ProcessingTask)
            .where(m.ProcessingTask.package_id == package_id)
            .order_by(
                m.ProcessingTask.created_at.desc(), m.ProcessingTask.stage_order
            )
        ).all()
        return {"items": [public_task(item) for item in items], "total": len(items)}

    @app.get("/api/speakers", tags=["说话人校验"])
    def list_speakers(
        session: DbSession,
        _user: CurrentUser,
        package_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        query = select(m.SpeakerCluster).where(visible_speaker_clause())
        if package_id:
            query = query.where(m.SpeakerCluster.package_id == package_id)
        if status:
            query = query.where(m.SpeakerCluster.status == status)
        items = session.scalars(
            query.order_by(m.SpeakerCluster.package_id, m.SpeakerCluster.label)
        ).all()
        return {
            "items": [public_speaker(session, item) for item in items],
            "total": len(items),
        }

    @app.get("/api/speakers/{speaker_id}", tags=["说话人校验"])
    def get_speaker(
        speaker_id: str, session: DbSession, _user: CurrentUser
    ) -> dict:
        item = _get_visible_speaker_or_404(session, speaker_id)
        return public_speaker(session, item)

    @app.patch("/api/speakers/{speaker_id}/confirm", tags=["说话人校验"])
    @app.post("/api/speakers/{speaker_id}/confirm", tags=["说话人校验"])
    def confirm_speaker_identity(
        speaker_id: str,
        payload: SpeakerConfirmRequest,
        session: DbSession,
        user: EditorUser,
    ) -> dict:
        item = _get_visible_speaker_or_404(session, speaker_id)
        trusted_payload = payload.model_copy(update={"actor": audit_actor(user)})
        confirmed = confirm_speaker(session, item, trusted_payload)
        return public_speaker(session, confirmed)

    @app.get("/api/intent-observations", tags=["意向确认"])
    def list_intent_observations(
        session: DbSession,
        _user: CurrentUser,
        package_id: str | None = None,
        status: str | None = None,
        customer_id: str | None = None,
        technology_id: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        query = select(m.RawIntentObservation)
        count_query = select(func.count()).select_from(m.RawIntentObservation)
        conditions = [visible_observation_clause()]
        if package_id:
            conditions.append(m.RawIntentObservation.package_id == package_id)
        if status:
            conditions.append(m.RawIntentObservation.status == status)
        if customer_id:
            conditions.append(m.RawIntentObservation.customer_id == customer_id)
        if technology_id:
            conditions.append(
                m.RawIntentObservation.technology_id == technology_id
            )
        query = query.where(*conditions)
        count_query = count_query.where(*conditions)
        items = session.scalars(
            query.order_by(m.RawIntentObservation.created_at.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return {
            "items": [public_observation(session, item) for item in items],
            "total": session.scalar(count_query) or 0,
            "offset": offset,
            "limit": limit,
        }

    @app.get("/api/intent-observations/{observation_id}", tags=["意向确认"])
    def get_intent_observation(
        observation_id: str, session: DbSession, _user: CurrentUser
    ) -> dict:
        item = _get_visible_observation_or_404(session, observation_id)
        return public_observation(session, item)

    @app.patch("/api/intent-observations/{observation_id}", tags=["意向确认"])
    def update_intent_observation(
        observation_id: str,
        payload: ObservationUpdateRequest,
        session: DbSession,
        user: EditorUser,
    ) -> dict:
        item = _get_visible_observation_or_404(session, observation_id)
        if item.version != payload.version:
            raise HTTPException(
                status_code=409,
                detail=f"意向观察已更新，请刷新后重试（当前版本{item.version}）",
            )
        if payload.function_id:
            _get_or_404(
                session, m.CustomerFunction, payload.function_id, "客户职能"
            )
        ensure_ai_snapshot(item)
        before = observation_review_snapshot(item)
        changes = payload.model_dump(
            exclude_unset=True, exclude={"version"}
        )
        for field, value in changes.items():
            setattr(item, field, value)
        if (
            "status" not in changes
            and before["status"] in {"已确认", "无法判断", "已被后续版本取代"}
        ):
            item.status = "待确认"
        item.version += 1
        package = session.get(m.MeetingPackage, item.package_id)
        if package:
            if item.status == "退回补充":
                package.status = "待补资料／待标注"
            elif item.status in {"AI草稿", "待确认"}:
                package.status = "待意向确认"
            package.confirmed_by = None
            package.confirmed_at = None
            package.version += 1
        session.add(
            m.ConfirmationRecord(
                target_type="raw_intent_observation",
                target_id=item.id,
                action="人工整理",
                before_snapshot=before,
                after_snapshot=observation_review_snapshot(item),
                actor=audit_actor(user),
                note="修改AI草稿，尚未发布正式状态",
            )
        )
        session.commit()
        session.refresh(item)
        return public_observation(session, item)

    @app.put(
        "/api/intent-observations/{observation_id}/review-draft",
        tags=["意向确认"],
    )
    def update_intent_review_draft(
        observation_id: str,
        payload: ObservationReviewDraftRequest,
        session: DbSession,
        user: EditorUser,
    ) -> dict:
        """Save reviewed intent fields and its platform path atomically."""

        item = _get_visible_observation_or_404(session, observation_id)
        if item.version != payload.version:
            raise HTTPException(
                status_code=409,
                detail=f"意向观察已更新，请刷新后重试（当前版本{item.version}）",
            )
        if payload.function_id:
            _get_or_404(
                session, m.CustomerFunction, payload.function_id, "客户职能"
            )

        ensure_ai_snapshot(item)
        before = observation_review_snapshot(item)
        changes = payload.model_dump(
            exclude_unset=True,
            exclude={"version", "platform"},
        )
        for field, value in changes.items():
            setattr(item, field, value)
        if (
            "status" not in changes
            and before["status"] in {"已确认", "无法判断", "已被后续版本取代"}
        ):
            item.status = "待确认"

        draft = None
        if payload.platform is not None:
            selection = payload.platform
            try:
                draft = propose_observation_platform(
                    session,
                    observation_id=observation_id,
                    technology_id=selection.technology_id,
                    level2_value=(
                        selection.level2_value_id
                        or selection.level2_new_value
                        or ""
                    ),
                    level3_value=(
                        selection.level3_value_id
                        or selection.level3_new_value
                        or ""
                    ),
                    level2_value_is_id=bool(selection.level2_value_id),
                    level3_value_is_id=bool(selection.level3_value_id),
                    actor=audit_actor(user),
                )
            except TaxonomyServiceError as exc:
                session.rollback()
                raise DomainError(422, f"{exc.code}：{exc.message}") from exc

        item.version += 1
        package = session.get(m.MeetingPackage, item.package_id)
        if package:
            if item.status == "退回补充":
                package.status = "待补资料／待标注"
            elif item.status in {"AI草稿", "待确认"}:
                package.status = "待意向确认"
            package.confirmed_by = None
            package.confirmed_at = None
            package.version += 1
        after = observation_review_snapshot(item)
        if draft is not None:
            after = {**after, "platform_draft": draft}
        session.add(
            m.ConfirmationRecord(
                target_type="raw_intent_observation",
                target_id=item.id,
                action="人工整理（含平台路径）" if draft else "人工整理",
                before_snapshot=before,
                after_snapshot=after,
                actor=audit_actor(user),
                note=(
                    "意向字段与平台路径在同一事务中保存；新增平台内容随正式确认激活"
                    if draft
                    else "修改AI草稿，尚未发布正式状态"
                ),
            )
        )
        try:
            session.commit()
        except Exception:
            session.rollback()
            raise
        session.refresh(item)
        return {
            "observation": public_observation(session, item),
            "platform_draft": draft,
            "message": (
                "意向字段与平台路径已原子保存。"
                if draft
                else "意向字段已保存。"
            ),
        }

    @app.post(
        "/api/intent-observations/{observation_id}/platform-draft",
        tags=["意向确认"],
    )
    def save_platform_draft(
        observation_id: str,
        payload: PlatformDraftRequest,
        session: DbSession,
        user: EditorUser,
    ) -> dict:
        item = _get_visible_observation_or_404(session, observation_id)
        if item.version != payload.version:
            raise DomainError(
                409,
                f"意向观察已更新，请刷新后重试（当前版本{item.version}）",
            )
        before = observation_review_snapshot(item)
        try:
            draft = propose_observation_platform(
                session,
                observation_id=observation_id,
                technology_id=payload.technology_id,
                level2_value=(
                    payload.level2_value_id or payload.level2_new_value or ""
                ),
                level3_value=(
                    payload.level3_value_id or payload.level3_new_value or ""
                ),
                level2_value_is_id=bool(payload.level2_value_id),
                level3_value_is_id=bool(payload.level3_value_id),
                actor=audit_actor(user),
            )
        except TaxonomyServiceError as exc:
            raise DomainError(422, f"{exc.code}：{exc.message}") from exc
        item.version += 1
        session.add(
            m.ConfirmationRecord(
                target_type="raw_intent_observation",
                target_id=item.id,
                action="平台路径整理",
                before_snapshot=before,
                after_snapshot={
                    **observation_review_snapshot(item),
                    "platform_draft": draft,
                },
                actor=audit_actor(user),
                note="新增内容保持待发布，随本条意向正式确认后激活",
            )
        )
        session.commit()
        session.refresh(item)
        return {
            "observation": public_observation(session, item),
            "platform_draft": draft,
            "message": "平台路径已保存；新增内容将在本条意向正式确认后启用。",
        }

    @app.post(
        "/api/intent-observations/{observation_id}/confirm",
        tags=["意向确认"],
    )
    def confirm_intent(
        observation_id: str,
        payload: IntentConfirmRequest,
        session: DbSession,
        user: EditorUser,
    ) -> dict:
        item = _get_visible_observation_or_404(session, observation_id)
        trusted_payload = payload.model_copy(update={"actor": audit_actor(user)})
        observation, formal = confirm_observation(
            session, item, trusted_payload
        )
        return {
            "observation": public_observation(session, observation),
            "formal_status": public_formal_status(session, formal),
            "message": "已由产品规划负责人确认并生成新的正式状态版本。",
        }

    @app.get("/api/formal-intent-statuses", tags=["意向查询"])
    def list_formal_statuses(
        session: DbSession,
        _user: CurrentUser,
        current_only: bool = True,
        customer_id: str | None = None,
        technology_id: str | None = None,
        function_id: str | None = None,
    ) -> dict:
        query = select(m.FormalIntentStatus)
        if current_only:
            query = query.where(m.FormalIntentStatus.is_current.is_(True))
        if customer_id:
            query = query.where(m.FormalIntentStatus.customer_id == customer_id)
        if technology_id:
            query = query.where(
                m.FormalIntentStatus.technology_id == technology_id
            )
        if function_id:
            query = query.where(m.FormalIntentStatus.function_id == function_id)
        items = session.scalars(
            query.order_by(
                m.FormalIntentStatus.effective_at.desc(),
                m.FormalIntentStatus.version_no.desc(),
            )
        ).all()
        return {
            "items": [public_formal_status(session, item) for item in items],
            "total": len(items),
        }

    @app.get("/api/evidence/{evidence_id}", tags=["证据"])
    def get_evidence(
        evidence_id: str, session: DbSession, _user: CurrentUser
    ) -> dict:
        item = _get_or_404(session, m.EvidenceFragment, evidence_id, "证据")
        return public_evidence(session, item)

    @app.get("/api/evidence/{evidence_id}/content", tags=["证据"])
    def get_evidence_content(
        evidence_id: str,
        session: DbSession,
        user: CurrentUser,
        source: bool = Query(
            default=False, description="true时返回受控原始文件；默认只返回证据片段"
        ),
        ):
        item = _get_or_404(session, m.EvidenceFragment, evidence_id, "证据")
        if not source or not item.material_file_id:
            return {
                "id": item.id,
                "locator": item.locator,
                "excerpt": item.excerpt,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "page_no": item.page_no,
                "frame_no": item.frame_no,
            }
        if user.role not in {ROLE_ADMIN, ROLE_USER}:
            raise HTTPException(
                status_code=403,
                detail="只读账户无权下载原始会议材料",
            )
        material = _get_or_404(
            session, m.MaterialFile, item.material_file_id, "原始材料"
        )
        path = (settings.data_dir / material.relative_path).resolve()
        if settings.data_dir.resolve() not in path.parents or not path.is_file():
            raise HTTPException(status_code=404, detail="原始文件不可用")
        return FileResponse(
            path,
            filename=material.safe_name,
            media_type=material.media_type or "application/octet-stream",
        )

    @app.get("/api/dashboard/summary", tags=["看板"])
    def get_dashboard_summary(
        session: DbSession, _user: CurrentUser
    ) -> dict:
        return dashboard_summary(session)

    @app.get("/api/dashboard/technology-matrix", tags=["看板"])
    def get_technology_matrix(
        session: DbSession,
        _user: CurrentUser,
        technology_id: str | None = None,
        customer_id: str | None = None,
    ) -> dict:
        return dashboard_matrix(
            session,
            technology_id=technology_id,
            customer_id=customer_id,
        )

    @app.get("/api/dashboard/customer-matrix", tags=["看板"])
    def get_customer_matrix(
        session: DbSession,
        _user: CurrentUser,
        customer_id: str | None = None,
        technology_id: str | None = None,
    ) -> dict:
        return dashboard_matrix(
            session,
            customer_id=customer_id,
            technology_id=technology_id,
        )

    @app.get("/api/history", tags=["历史"])
    def history(
        session: DbSession,
        _user: CurrentUser,
        customer_id: str | None = None,
        technology_id: str | None = None,
        function_id: str | None = None,
        status_key: str | None = None,
    ) -> dict:
        query = select(m.FormalIntentStatus)
        if customer_id:
            query = query.where(m.FormalIntentStatus.customer_id == customer_id)
        if technology_id:
            query = query.where(
                m.FormalIntentStatus.technology_id == technology_id
            )
        if function_id:
            query = query.where(m.FormalIntentStatus.function_id == function_id)
        if status_key:
            query = query.where(m.FormalIntentStatus.status_key == status_key)
        items = session.scalars(
            query.order_by(
                m.FormalIntentStatus.effective_at.desc(),
                m.FormalIntentStatus.version_no.desc(),
            )
        ).all()
        return {
            "items": [public_formal_status(session, item) for item in items],
            "total": len(items),
            "data_scope": "模拟数据",
        }

    @app.get("/api/meta/attitudes", tags=["系统"])
    def attitude_dictionary(_user: CurrentUser) -> dict:
        return {
            "items": [
                {"level": level, "label": label}
                for level, label in ATTITUDE_LABELS.items()
            ]
        }

    return app


app = create_app()
