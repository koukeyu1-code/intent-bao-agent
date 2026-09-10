from __future__ import annotations

import logging
import queue
import threading
from typing import Any

from sqlalchemy import or_, select

from . import models as m
from .config import Settings
from .database import Database
from .model_adapters import get_model_adapter
from .model_settings import ModelSettingsStore
from .services import (
    execute_analysis_run,
    finalize_analysis_run_cancellation,
)
from .stage_adapters import get_stage_adapter

logger = logging.getLogger(__name__)

_SENTINEL = object()


class AnalysisQueue:
    """Single background worker that executes analysis runs serially.

    API routes persist the run and its eight stage tasks, then call
    ``enqueue`` and return HTTP 202 immediately.  A dedicated daemon thread
    consumes run ids one at a time so long model/ASR calls never occupy
    FastAPI request threads.
    """

    def __init__(
        self,
        database: Database,
        model_settings: ModelSettingsStore,
        settings: Settings,
    ) -> None:
        self._database = database
        self._model_settings = model_settings
        self._settings = settings
        self._queue: queue.Queue[Any] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._queued_or_running: set[str] = set()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="intent-bao-analysis-worker",
            daemon=True,
        )
        self._thread.start()
        logger.info("后台分析Worker已启动")

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._queue.put(_SENTINEL)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def enqueue(
        self,
        run_id: str,
        *,
        adapter=None,
        stage_adapter=None,
        asr_settings=None,
    ) -> None:
        # Lazy start also covers TestClient usage where the ASGI lifespan
        # never runs; the lifespan additionally starts/stops the worker.
        self.start()
        with self._lock:
            if run_id in self._queued_or_running:
                return
            self._queued_or_running.add(run_id)
        self._queue.put(
            (run_id, adapter, stage_adapter, asr_settings)
        )

    def contains(self, run_id: str) -> bool:
        with self._lock:
            return run_id in self._queued_or_running

    def _run_loop(self) -> None:
        while True:
            if self._stop_event.is_set():
                return
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is _SENTINEL:
                return
            run_id, adapter, stage_adapter, asr_settings = item
            try:
                self._process(
                    run_id,
                    adapter=adapter,
                    stage_adapter=stage_adapter,
                    asr_settings=asr_settings,
                )
            except Exception:
                logger.exception("分析任务后台执行失败: run=%s", run_id)
            finally:
                with self._lock:
                    self._queued_or_running.discard(run_id)
                self._queue.task_done()

    def _resolve_adapters(self, run: m.AnalysisRun):
        """Rebuild intent/stage adapters for the run from current settings."""
        if run.adapter == "mock":
            return get_model_adapter("mock"), None, None
        active = self._model_settings.snapshot()
        adapter = get_model_adapter(
            run.adapter,
            base_url=active.base_url,
            model_name=active.model_name,
            api_key=active.api_key,
        )
        stage_adapter = get_stage_adapter(
            active.provider,
            base_url=active.base_url,
            model_name=active.model_name,
            api_key=active.api_key,
        )
        return adapter, stage_adapter, active.asr

    def _process(
        self,
        run_id: str,
        *,
        adapter=None,
        stage_adapter=None,
        asr_settings=None,
    ) -> None:
        with self._database.session() as session:
            run = session.get(m.AnalysisRun, run_id)
            if run is None:
                return
            if run.cancel_requested_at is not None:
                finalize_analysis_run_cancellation(session, run)
                return
            if run.status in {"已取消", "已完成", "处理失败"}:
                return
            try:
                if adapter is None:
                    adapter, stage_adapter, asr_settings = (
                        self._resolve_adapters(run)
                    )
                execute_analysis_run(
                    session,
                    run,
                    settings=self._settings,
                    adapter=adapter,
                    stage_adapter=stage_adapter,
                    asr_settings=asr_settings,
                )
            except Exception as exc:
                # Resolution or execution failures must surface as a failed
                # run so polling clients see a terminal state, not a hang.
                session.rollback()
                current = session.get(m.AnalysisRun, run_id)
                if current is not None and (
                    current.cancel_requested_at is not None
                    or current.status == "已取消"
                ):
                    finalize_analysis_run_cancellation(session, current)
                    return
                if (
                    current is not None
                    and current.status not in {"已取消", "已完成", "处理失败"}
                ):
                    current.status = "处理失败"
                    current.error_message = str(exc)[:500]
                    current.finished_at = current.finished_at or m.utcnow()
                    package = session.get(
                        m.MeetingPackage, current.package_id
                    )
                    if package is not None:
                        package.status = "处理失败"
                        package.last_error = str(exc)[:500]
                        package.version += 1
                    open_tasks = session.scalars(
                        select(m.ProcessingTask).where(
                            m.ProcessingTask.analysis_run_id == run_id,
                            m.ProcessingTask.status.in_(
                                ("待处理", "处理中")
                            ),
                        )
                    ).all()
                    for task in open_tasks:
                        task.status = "处理失败"
                        task.error_message = str(exc)[:500]
                        task.finished_at = m.utcnow()
                    session.commit()
                raise


def mark_stale_analysis_runs_failed(database: Database) -> int:
    """Fail runs left in ``运行中`` by a previous process at startup.

    A process restart cannot resume a partially executed stage pipeline, so
    the honest state is a clear failure with a retry prompt rather than a
    run that never finishes.
    """
    from . import models as models_module

    with database.session() as session:
        # A process may stop after the API persisted the cancellation marker
        # but before the worker reached its next safe boundary.  Sweep those
        # runs first so committed draft rows are cleaned on the next startup.
        cancelled_ids = list(
            session.scalars(
                select(models_module.AnalysisRun.id).where(
                    or_(
                        models_module.AnalysisRun.status == "已取消",
                        models_module.AnalysisRun.cancel_requested_at.is_not(None),
                    )
                )
            ).all()
        )
        for run_id in cancelled_ids:
            cancelled = session.get(models_module.AnalysisRun, run_id)
            if cancelled is not None:
                finalize_analysis_run_cancellation(session, cancelled)

        stale = list(
            session.scalars(
                select(models_module.AnalysisRun).where(
                    models_module.AnalysisRun.status == "运行中"
                )
            ).all()
        )
        for run in stale:
            finished_at = models_module.utcnow()
            run.status = "处理失败"
            run.error_message = "服务重启导致分析中断，请点击重试重新分析"
            run.finished_at = run.finished_at or finished_at
            open_tasks = session.scalars(
                select(models_module.ProcessingTask).where(
                    models_module.ProcessingTask.analysis_run_id == run.id,
                    models_module.ProcessingTask.status.in_(("待处理", "处理中")),
                )
            ).all()
            for task in open_tasks:
                task.status = "处理失败"
                task.error_message = run.error_message
                task.finished_at = task.finished_at or finished_at
            package = session.get(models_module.MeetingPackage, run.package_id)
            if package is not None and package.status == "分析中":
                package.status = "处理失败"
                package.last_error = run.error_message
                package.version += 1
        session.commit()
        return len(stale)
