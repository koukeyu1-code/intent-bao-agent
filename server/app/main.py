from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

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
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import models as m
from .config import Settings
from .database import Database, get_db
from .domain import ATTITUDE_LABELS
from .model_adapters import (
    ModelAdapterError,
    OllamaIntentModelAdapter,
    get_model_adapter,
)
from .model_settings import ModelSettingsError, ModelSettingsStore
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
    IntentConfirmRequest,
    MeetingPackageCreate,
    MeetingPackageUpdate,
    ModelConnectionRequest,
    ModelConnectionTestResponse,
    ModelListResponse,
    ModelSettingsResponse,
    ModelSettingsUpdateRequest,
    ObservationUpdateRequest,
    SpeakerConfirmRequest,
)
from .seed import seed_demo_data
from .services import (
    DomainError,
    confirm_observation,
    confirm_speaker,
    ensure_ai_snapshot,
    observation_review_snapshot,
    run_analysis,
    save_uploaded_file,
)


DbSession = Annotated[Session, Depends(get_db)]


def _get_or_404(session: Session, model, item_id: str, label: str):
    item = session.get(model, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail=f"{label}不存在")
    return item


def create_app(
    *,
    database_url: str | None = None,
    data_dir: str | Path | None = None,
    seed: bool = True,
) -> FastAPI:
    settings = Settings.load(database_url=database_url, data_dir=data_dir)
    model_settings = ModelSettingsStore(settings.data_dir)
    database = Database(settings.database_url)
    database.create_all()
    if seed:
        with database.session() as session:
            seed_demo_data(session, settings)

    app = FastAPI(
        title="意向宝智能体 API",
        summary="把客户会议材料整理成可追溯、可确认的产品规划意向地图",
        description=(
            "首版为本地单用户模式，默认使用清晰标识的模拟数据和 mock 分析器。"
            "真实模型接口已预留，但未启用，也未宣称达到85%准确率。"
        ),
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/api/openapi.json",
    )
    app.state.settings = settings
    app.state.model_settings = model_settings
    app.state.database = database

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_credentials=True,
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
            "data_scope": "本地单用户",
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
    def get_model_settings() -> dict:
        return model_settings.public_dict()

    @app.put(
        "/api/model-settings",
        tags=["模型设置"],
        response_model=ModelSettingsResponse,
    )
    def update_model_settings(payload: ModelSettingsUpdateRequest) -> dict:
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
            )
        except ModelSettingsError as exc:
            raise DomainError(422, str(exc)) from exc
        return updated.public_dict()

    @app.post(
        "/api/model-settings/test",
        tags=["模型设置"],
        response_model=ModelConnectionTestResponse,
    )
    def test_model_connection(
        payload: ModelConnectionRequest,
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
    def list_model_options(payload: ModelConnectionRequest) -> dict:
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
    def bootstrap(session: DbSession) -> dict:
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
                "mode": "本地单用户",
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
    def master_data(session: DbSession) -> dict:
        data = public_master_data(session)
        return {
            **data,
            "data_scope": "模拟数据",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/meeting-packages", tags=["会议资料包"])
    def list_meeting_packages(
        session: DbSession,
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
        payload: MeetingPackageCreate, session: DbSession
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
    def get_meeting_package(package_id: str, session: DbSession) -> dict:
        item = _get_or_404(session, m.MeetingPackage, package_id, "会议资料包")
        return public_package(session, item, include_detail=True)

    @app.patch("/api/meeting-packages/{package_id}", tags=["会议资料包"])
    def update_meeting_package(
        package_id: str, payload: MeetingPackageUpdate, session: DbSession
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
        package_id: str, payload: AnalyzeRequest, session: DbSession
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
        if adapter_name != "mock":
            try:
                adapter = get_model_adapter(
                    adapter_name,
                    base_url=active_model.base_url,
                    model_name=active_model.model_name,
                    api_key=active_model.api_key,
                )
            except (ModelSettingsError, ValueError) as exc:
                raise DomainError(422, str(exc)) from exc
        run = run_analysis(
            session,
            package,
            adapter_name=adapter_name,
            force=payload.force,
            settings=settings,
            adapter=adapter,
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
                "started_at": iso(run.started_at),
                "finished_at": iso(run.finished_at),
            },
            "stages": [public_task(item) for item in tasks],
            "message": (
                "模拟分析已完成，结果仅用于流程演示。"
                if run.status == "已完成" and run.adapter == "mock"
                else (
                    f"Ollama分析已完成，使用模型 {run.model_name}；"
                    "结果为待人工确认的AI草稿。"
                )
                if run.status == "已完成" and run.adapter == "ollama"
                else "任务已创建。"
            ),
        }

    @app.get(
        "/api/meeting-packages/{package_id}/tasks", tags=["处理任务"]
    )
    def list_package_tasks(package_id: str, session: DbSession) -> dict:
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
        package_id: str | None = None,
        status: str | None = None,
    ) -> dict:
        query = select(m.SpeakerCluster)
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
    def get_speaker(speaker_id: str, session: DbSession) -> dict:
        item = _get_or_404(session, m.SpeakerCluster, speaker_id, "说话人")
        return public_speaker(session, item)

    @app.patch("/api/speakers/{speaker_id}/confirm", tags=["说话人校验"])
    @app.post("/api/speakers/{speaker_id}/confirm", tags=["说话人校验"])
    def confirm_speaker_identity(
        speaker_id: str, payload: SpeakerConfirmRequest, session: DbSession
    ) -> dict:
        item = _get_or_404(session, m.SpeakerCluster, speaker_id, "说话人")
        confirmed = confirm_speaker(session, item, payload)
        return public_speaker(session, confirmed)

    @app.get("/api/intent-observations", tags=["意向确认"])
    def list_intent_observations(
        session: DbSession,
        package_id: str | None = None,
        status: str | None = None,
        customer_id: str | None = None,
        technology_id: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict:
        query = select(m.RawIntentObservation)
        count_query = select(func.count()).select_from(m.RawIntentObservation)
        conditions = []
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
        if conditions:
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
        observation_id: str, session: DbSession
    ) -> dict:
        item = _get_or_404(
            session, m.RawIntentObservation, observation_id, "原始意向观察"
        )
        return public_observation(session, item)

    @app.patch("/api/intent-observations/{observation_id}", tags=["意向确认"])
    def update_intent_observation(
        observation_id: str,
        payload: ObservationUpdateRequest,
        session: DbSession,
    ) -> dict:
        item = _get_or_404(
            session, m.RawIntentObservation, observation_id, "原始意向观察"
        )
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
                actor="本地产品规划人员",
                note="修改AI草稿，尚未发布正式状态",
            )
        )
        session.commit()
        session.refresh(item)
        return public_observation(session, item)

    @app.post(
        "/api/intent-observations/{observation_id}/confirm",
        tags=["意向确认"],
    )
    def confirm_intent(
        observation_id: str,
        payload: IntentConfirmRequest,
        session: DbSession,
    ) -> dict:
        item = _get_or_404(
            session, m.RawIntentObservation, observation_id, "原始意向观察"
        )
        observation, formal = confirm_observation(session, item, payload)
        return {
            "observation": public_observation(session, observation),
            "formal_status": public_formal_status(session, formal),
            "message": "已由产品规划负责人确认并生成新的正式状态版本。",
        }

    @app.get("/api/formal-intent-statuses", tags=["意向查询"])
    def list_formal_statuses(
        session: DbSession,
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
    def get_evidence(evidence_id: str, session: DbSession) -> dict:
        item = _get_or_404(session, m.EvidenceFragment, evidence_id, "证据")
        return public_evidence(session, item)

    @app.get("/api/evidence/{evidence_id}/content", tags=["证据"])
    def get_evidence_content(
        evidence_id: str,
        session: DbSession,
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
    def get_dashboard_summary(session: DbSession) -> dict:
        return dashboard_summary(session)

    @app.get("/api/dashboard/technology-matrix", tags=["看板"])
    def get_technology_matrix(
        session: DbSession,
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
    def attitude_dictionary() -> dict:
        return {
            "items": [
                {"level": level, "label": label}
                for level, label in ATTITUDE_LABELS.items()
            ]
        }

    return app


app = create_app()
