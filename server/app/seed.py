from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models as m
from .config import Settings


TASK_STAGES = [
    "文件预检",
    "文档解析",
    "音轨与转写",
    "说话人分离",
    "画面与PDF解析",
    "资料对齐",
    "身份辅助匹配",
    "意向抽取",
]


def _dt(day: int, hour: int = 9) -> datetime:
    return datetime(2026, 7, day, hour, tzinfo=timezone.utc)


def _write_demo_material(
    settings: Settings, package_id: str, filename: str, content: str
) -> tuple[str, str, int]:
    folder = settings.projects_dir / "project-mnt" / package_id / "source"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / filename
    path.write_text(content, encoding="utf-8")
    raw = content.encode("utf-8")
    relative = path.relative_to(settings.data_dir).as_posix()
    return relative, hashlib.sha256(raw).hexdigest(), len(raw)


def seed_demo_data(session: Session, settings: Settings) -> None:
    if session.scalar(select(func.count()).select_from(m.Project)):
        return

    project = m.Project(
        id="project-mnt",
        name="MNT客户意向地图试点",
        description="面向显示器产品企划团队的证据化客户意向智能体首期试点。",
        data_scope="模拟数据",
        retention_days=None,
    )
    customers = [
        m.Customer(id=f"customer-{key.lower()}", code=key, name=f"客户{key}", display_name=f"客户{key}")
        for key in "ABCDE"
    ]
    technologies = [
        m.Technology(id="tech-mini-led", code="MINI_LED", name="Mini LED", category="显示技术"),
        m.Technology(id="tech-three-mode", code="THREE_MODE", name="三模", category="产品功能"),
        m.Technology(id="tech-wide-color", code="WIDE_COLOR", name="高色域", category="显示技术"),
        m.Technology(id="tech-aglr", code="AGLR", name="AGLR", category="光学技术"),
        m.Technology(
            id="tech-circular-polarizer",
            code="CIRCULAR_POLARIZER",
            name="圆偏光",
            category="光学技术",
        ),
        m.Technology(id="tech-stw", code="STW", name="STW", category="光学技术"),
    ]
    platforms = [
        m.GenericPlatform(
            id="platform-238",
            code="P238",
            name="23.8英寸产品平台",
            size_inch=23.8,
            resolution="FHD/QHD",
            product_series="主流办公",
        ),
        m.GenericPlatform(
            id="platform-27-5k",
            code="P27_5K",
            name="27英寸5K产品平台",
            size_inch=27,
            resolution="5K",
            product_series="高端专业",
        ),
        m.GenericPlatform(
            id="platform-315-6k",
            code="P315_6K",
            name="31.5英寸6K产品平台",
            size_inch=31.5,
            resolution="6K",
            product_series="高端专业",
        ),
    ]
    functions = [
        m.CustomerFunction(id="function-purchase", code="PURCHASE", name="采购", sort_order=10),
        m.CustomerFunction(id="function-development", code="DEVELOPMENT", name="开发", sort_order=20),
        m.CustomerFunction(id="function-pm", code="PM", name="PM", sort_order=30),
        m.CustomerFunction(id="function-product", code="PRODUCT", name="产品", sort_order=40),
        m.CustomerFunction(id="function-quality", code="QUALITY", name="品质", sort_order=50),
    ]
    customer_projects = [
        m.CustomerProject(
            id="customer-project-a-pro",
            customer_id="customer-a",
            generic_platform_id="platform-27-5k",
            code="A_PRO_5K",
            name="A-Pro 5K项目",
        ),
        m.CustomerProject(
            id="customer-project-b-fusion",
            customer_id="customer-b",
            generic_platform_id="platform-315-6k",
            code="B_FUSION_6K",
            name="B-Fusion 6K项目",
        ),
        m.CustomerProject(
            id="customer-project-c-color",
            customer_id="customer-c",
            generic_platform_id="platform-238",
            code="C_COLOR",
            name="C-Color平台",
        ),
        m.CustomerProject(
            id="customer-project-e-optics",
            customer_id="customer-e",
            generic_platform_id="platform-315-6k",
            code="E_OPTICS",
            name="E-Optics项目",
        ),
    ]
    aliases = [
        m.Alias(id="alias-mini", entity_type="technology", entity_id="tech-mini-led", alias="MiniLED"),
        m.Alias(id="alias-three", entity_type="technology", entity_id="tech-three-mode", alias="三模式"),
        m.Alias(id="alias-315", entity_type="platform", entity_id="platform-315-6k", alias="32寸6K"),
        m.Alias(id="alias-product", entity_type="function", entity_id="function-product", alias="产品规划"),
    ]
    session.add(project)
    session.add_all(customers + technologies + platforms + functions)
    session.flush()
    session.add_all(customer_projects + aliases)
    session.flush()

    package_specs = [
        (
            "package-a-old",
            "customer-a",
            "客户A Mini LED早期需求沟通",
            date(2026, 6, 25),
            "已确认",
            "完整",
        ),
        (
            "package-a-new",
            "customer-a",
            "客户A Mini LED × 27英寸5K方案评审",
            date(2026, 7, 18),
            "已确认",
            "完整",
        ),
        (
            "package-b",
            "customer-b",
            "客户B 三模 × 31.5英寸6K产品交流",
            date(2026, 7, 22),
            "待意向确认",
            "完整",
        ),
        (
            "package-c",
            "customer-c",
            "客户C 高色域与AGLR技术讨论",
            date(2026, 7, 24),
            "待身份校验",
            "资料不完整",
        ),
        (
            "package-d",
            "customer-d",
            "客户D Mini LED成本与品质专题",
            date(2026, 7, 27),
            "分析中",
            "完整",
        ),
        (
            "package-e",
            "customer-e",
            "客户E 圆偏光与STW路线规划会",
            date(2026, 7, 29),
            "已确认",
            "完整",
        ),
    ]
    packages: list[m.MeetingPackage] = []
    for package_id, customer_id, title, meeting_date, status, completeness in package_specs:
        packages.append(
            m.MeetingPackage(
                id=package_id,
                project_id=project.id,
                customer_id=customer_id,
                title=title,
                meeting_date=meeting_date,
                data_scope="模拟数据",
                status=status,
                material_completeness=completeness,
                notes="仅用于功能流程展示，不代表真实客户结论。",
                confirmed_by="产品规划负责人" if status == "已确认" else None,
                confirmed_at=_dt(min(meeting_date.day + 1, 30)) if status == "已确认" else None,
            )
        )
    session.add_all(packages)
    session.flush()

    # Physical UTF-8 demo notes prove the controlled file-storage path.
    demo_notes = {
        "package-a-new": (
            "客户A Mini LED × 27英寸5K方案评审纪要（模拟）\n"
            "产品规划：可以纳入路线图候选，先推进样机验证。\n"
            "开发：8月可安排兼容性验证。\n"
            "采购：需要把背光成本增幅控制在目标范围内。\n"
            "品质：可靠性数据补齐后再进入正式评审。\n"
        ),
        "package-b": (
            "客户B 三模 × 31.5英寸6K产品交流纪要（模拟）\n"
            "产品：三模有差异化价值。\n"
            "开发：建议先验证切换稳定性。\n"
            "采购：当前成本仍有压力。\n"
        ),
        "package-e": (
            "客户E 圆偏光与STW路线规划会纪要（模拟）\n"
            "产品规划希望比较两条光学路线，品质提出环境可靠性门槛。\n"
        ),
    }
    materials: list[m.MaterialFile] = []
    for package_id, content in demo_notes.items():
        relative, digest, size = _write_demo_material(
            settings, package_id, f"{package_id}-minutes.txt", content
        )
        materials.append(
            m.MaterialFile(
                id=f"material-{package_id}",
                package_id=package_id,
                original_name=f"{package_id}-会议纪要.txt",
                safe_name=f"{package_id}-minutes.txt",
                media_type="text/plain",
                material_type="人工纪要",
                relative_path=relative,
                sha256=digest,
                size_bytes=size,
                parse_status="解析完成",
            )
        )
    session.add_all(materials)
    session.flush()

    attendee_specs = [
        ("a-product", "package-a-new", "赵总", "产品规划负责人", "产品规划部", "function-product"),
        ("a-dev", "package-a-new", "钱工", "开发经理", "开发部", "function-development"),
        ("a-buy", "package-a-new", "孙经理", "采购经理", "采购部", "function-purchase"),
        ("a-quality", "package-a-new", "李工", "品质工程师", "品质部", "function-quality"),
        ("b-product", "package-b", "周总", "产品规划负责人", "产品部", "function-product"),
        ("b-dev", "package-b", "吴工", "开发工程师", "开发部", "function-development"),
        ("b-buy", "package-b", "郑经理", "采购经理", "采购部", "function-purchase"),
        ("c-unknown", "package-c", "王工", "待确认", "客户C", None),
        ("c-pm", "package-c", "李经理", "项目经理", "PM部", "function-pm"),
        ("e-product", "package-e", "赵经理", "产品经理", "产品部", "function-product"),
        ("e-quality", "package-e", "钱工", "品质工程师", "品质部", "function-quality"),
    ]
    attendees = [
        m.MeetingAttendee(
            id=f"attendee-{key}",
            package_id=package_id,
            internal_person_id=f"person-{key}",
            display_name=name,
            role=role,
            organization=org,
            function_id=function_id,
            is_confirmed=function_id is not None,
        )
        for key, package_id, name, role, org, function_id in attendee_specs
    ]
    vault_items = [
        m.IdentityVault(
            id=f"vault-{key}",
            internal_person_id=f"person-{key}",
            real_name=None,
            masked_name=name,
            customer_id=next(
                customer_id
                for pid, customer_id, *_ in package_specs
                if pid == package_id
            ),
            organization=org,
            role=role,
            function_id=function_id,
        )
        for key, package_id, name, role, org, function_id in attendee_specs
    ]
    session.add_all(attendees + vault_items)
    session.flush()

    run_specs = [
        ("run-a-old", "package-a-old", "已完成"),
        ("run-a-new", "package-a-new", "已完成"),
        ("run-b", "package-b", "已完成"),
        ("run-c", "package-c", "已完成"),
        ("run-d", "package-d", "运行中"),
        ("run-e", "package-e", "已完成"),
    ]
    for run_id, package_id, status in run_specs:
        run = m.AnalysisRun(
            id=run_id,
            package_id=package_id,
            adapter="mock",
            model_name="mock-intent-v1",
            status=status,
            started_at=_dt(28, 8),
            finished_at=_dt(28, 8) if status == "已完成" else None,
            result_summary={
                "data_scope": "模拟数据",
                "notice": "流程演示结果，不代表真实模型准确率",
            },
        )
        session.add(run)
        session.flush()
        for index, stage in enumerate(TASK_STAGES, 1):
            stage_completed = status == "已完成" or index <= 4
            session.add(
                m.ProcessingTask(
                    id=f"task-{run_id}-{index}",
                    package_id=package_id,
                    analysis_run_id=run_id,
                    stage=stage,
                    stage_order=index,
                    status="已完成" if stage_completed else ("处理中" if index == 5 else "待处理"),
                    progress=100 if stage_completed else (45 if index == 5 else 0),
                    attempt_count=1 if stage_completed or index == 5 else 0,
                    started_at=_dt(28, 8) if stage_completed or index == 5 else None,
                    finished_at=_dt(28, 8) if stage_completed else None,
                )
            )
    session.flush()

    speaker_specs = [
        ("speaker-a-product", "package-a-new", "Speaker A", "赵总", "产品规划负责人", "产品规划部", "function-product", 0.94, "已确认"),
        ("speaker-a-dev", "package-a-new", "Speaker B", "钱工", "开发经理", "开发部", "function-development", 0.88, "已确认"),
        ("speaker-a-buy", "package-a-new", "Speaker C", "孙经理", "采购经理", "采购部", "function-purchase", 0.86, "已确认"),
        ("speaker-a-quality", "package-a-new", "Speaker D", "李工", "品质工程师", "品质部", "function-quality", 0.82, "已确认"),
        ("speaker-b-product", "package-b", "Speaker A", "周总", "产品规划负责人", "产品部", "function-product", 0.90, "已确认"),
        ("speaker-b-dev", "package-b", "Speaker B", "吴工", "开发工程师", "开发部", "function-development", 0.81, "已确认"),
        ("speaker-b-buy", "package-b", "Speaker C", "郑经理", "采购经理", "采购部", "function-purchase", 0.79, "已确认"),
        ("speaker-c-unknown", "package-c", "Speaker A", "王工", "待确认", "客户C", None, 0.54, "待确认"),
        ("speaker-c-product", "package-c", "Speaker B", "未识别", "产品相关角色", "客户C", "function-product", 0.63, "待确认"),
        ("speaker-c-pm", "package-c", "Speaker C", "李经理", "项目经理", "PM部", "function-pm", 0.88, "已确认"),
        ("speaker-e-product", "package-e", "Speaker A", "赵经理", "产品经理", "产品部", "function-product", 0.91, "已确认"),
        ("speaker-e-quality", "package-e", "Speaker B", "钱工", "品质工程师", "品质部", "function-quality", 0.87, "已确认"),
    ]
    for (
        speaker_id,
        package_id,
        label,
        name,
        role,
        organization,
        function_id,
        confidence,
        status,
    ) in speaker_specs:
        speaker = m.SpeakerCluster(
            id=speaker_id,
            package_id=package_id,
            label=label,
            recommended_name=name,
            recommended_role=role,
            recommended_organization=organization,
            recommended_function_id=function_id,
            confirmed_name=name if status == "已确认" else None,
            confirmed_role=role if status == "已确认" else None,
            confirmed_organization=organization if status == "已确认" else None,
            confirmed_function_id=function_id if status == "已确认" else None,
            confidence=confidence,
            status=status,
        )
        session.add(speaker)
        session.flush()
        session.add(
            m.SpeakerSegment(
                id=f"segment-{speaker_id}",
                speaker_id=speaker_id,
                start_ms=120_000,
                end_ms=142_000,
                transcript=f"{name}关于本次技术路线的模拟发言片段。",
                confidence=confidence,
            )
        )
        if status == "已确认":
            session.add(
                m.SpeakerIdentityMapping(
                    id=f"mapping-{speaker_id}",
                    speaker_id=speaker_id,
                    display_name=name,
                    role=role,
                    organization=organization,
                    function_id=function_id,
                    identity_granularity="完整",
                )
            )
    session.flush()

    evidence_specs = [
        ("evidence-a-product-old", "package-a-old", None, "会议纪要", "第2节", "方向有兴趣，但需要先确认样机效果和整体成本。", None, 0.87),
        ("evidence-a-product", "package-a-new", "material-package-a-new", "会议纪要", "第2节", "如果样机验证通过，可以把Mini LED放进明年高端5K路线图。", None, 0.96),
        ("evidence-a-dev", "package-a-new", "material-package-a-new", "转写片段", "00:18:20–00:18:42", "开发侧建议先做27英寸5K样机，8月安排兼容性验证。", 1_100_000, 0.92),
        ("evidence-a-buy", "package-a-new", "material-package-a-new", "会议纪要", "第4节", "背光成本增幅需要控制在目标范围内，满足后再进入报价评估。", None, 0.91),
        ("evidence-a-quality", "package-a-new", "material-package-a-new", "会议纪要", "第5节", "需要补齐高温高湿和寿命数据，再进入品质评审。", None, 0.90),
        ("evidence-b-product", "package-b", "material-package-b", "会议纪要", "第2节", "三模对高端6K产品有差异化价值，可以放入路线图候选。", None, 0.93),
        ("evidence-b-dev", "package-b", "material-package-b", "转写片段", "00:26:05–00:26:36", "开发认可方向，但需要先验证模式切换稳定性。", 1_565_000, 0.87),
        ("evidence-b-buy", "package-b", "material-package-b", "会议纪要", "第4节", "目前方案成本仍高于预算，采购侧暂不支持直接导入。", None, 0.89),
        ("evidence-c-unknown", "package-c", None, "转写片段", "00:11:08–00:11:25", "这个色域方向可以再看看，但我还不能代表产品规划做结论。", 668_000, 0.58),
        ("evidence-c-aglr-pm", "package-c", None, "PDF页面", "第12页", "AGLR在当前23.8英寸平台会增加结构厚度，PM明确提出本代项目不导入。", None, 0.91),
        ("evidence-e-product", "package-e", "material-package-e", "会议纪要", "第3节", "圆偏光可以进入下一轮产品方案比较，优先看31.5英寸6K。", None, 0.90),
        ("evidence-e-quality", "package-e", "material-package-e", "录屏关键帧", "画面 00:42:16", "STW需要先完成环境可靠性验证，当前保持有条件观望。", None, 0.89),
    ]
    for evidence_id, package_id, material_id, evidence_type, locator, excerpt, start_ms, confidence in evidence_specs:
        session.add(
            m.EvidenceFragment(
                id=evidence_id,
                package_id=package_id,
                material_file_id=material_id,
                evidence_type=evidence_type,
                locator=locator,
                excerpt=excerpt,
                start_ms=start_ms,
                end_ms=start_ms + 30_000 if start_ms else None,
                page_no=12 if evidence_id == "evidence-c-aglr-pm" else None,
                frame_no=2536 if evidence_id == "evidence-e-quality" else None,
                confidence=confidence,
                playback_available=start_ms is not None,
            )
        )
    session.flush()

    observation_specs = [
        # id, package, run, customer, technology, platform, project, function, speaker, evidence,
        # attitude, stage, support, resistance, bottlenecks, influence, status, confidence
        ("obs-a-product-old", "package-a-old", "run-a-old", "customer-a", "tech-mini-led", "platform-27-5k", "customer-project-a-pro", "function-product", None, "evidence-a-product-old", 3, "初步了解", None, "需要确认样机效果和成本", ["性能", "成本"], "评估", "已被后续版本取代", 0.84),
        ("obs-a-product", "package-a-new", "run-a-new", "customer-a", "tech-mini-led", "platform-27-5k", "customer-project-a-pro", "function-product", "speaker-a-product", "evidence-a-product", 5, "产品项目规划", "产品差异化价值明确，主动建议进入路线图", None, [], "推动", "已确认", 0.93),
        ("obs-a-dev", "package-a-new", "run-a-new", "customer-a", "tech-mini-led", "platform-27-5k", "customer-project-a-pro", "function-development", "speaker-a-dev", "evidence-a-dev", 4, "样品／技术验证", "愿意安排样机与兼容性验证", None, ["验证资源"], "评估", "已确认", 0.90),
        ("obs-a-buy", "package-a-new", "run-a-new", "customer-a", "tech-mini-led", "platform-27-5k", "customer-project-a-pro", "function-purchase", "speaker-a-buy", "evidence-a-buy", 2, "内部评估", None, "成本增幅仍需收敛，当前暂不支持导入", ["成本"], "把关", "已确认", 0.88),
        ("obs-a-quality", "package-a-new", "run-a-new", "customer-a", "tech-mini-led", "platform-27-5k", "customer-project-a-pro", "function-quality", "speaker-a-quality", "evidence-a-quality", 3, "样品／技术验证", None, "可靠性数据尚不完整", ["品质", "可靠性"], "把关", "已确认", 0.86),
        ("obs-b-product", "package-b", "run-b", "customer-b", "tech-three-mode", "platform-315-6k", "customer-project-b-fusion", "function-product", "speaker-b-product", "evidence-b-product", 4, "产品项目规划", "认可高端产品差异化价值", None, [], "推动", "待确认", 0.91),
        ("obs-b-dev", "package-b", "run-b", "customer-b", "tech-three-mode", "platform-315-6k", "customer-project-b-fusion", "function-development", "speaker-b-dev", "evidence-b-dev", 3, "样品／技术验证", None, "需要验证模式切换稳定性", ["性能", "验证资源"], "评估", "待确认", 0.84),
        ("obs-b-buy", "package-b", "run-b", "customer-b", "tech-three-mode", "platform-315-6k", "customer-project-b-fusion", "function-purchase", "speaker-b-buy", "evidence-b-buy", 2, "内部评估", None, "成本高于预算", ["成本"], "把关", "待确认", 0.88),
        ("obs-c-unknown", "package-c", "run-c", "customer-c", "tech-wide-color", "platform-238", "customer-project-c-color", None, "speaker-c-unknown", "evidence-c-unknown", None, "初步了解", None, None, [], "未知", "AI草稿", 0.55),
        ("obs-c-aglr-pm", "package-c", "run-c", "customer-c", "tech-aglr", "platform-238", "customer-project-c-color", "function-pm", "speaker-c-pm", "evidence-c-aglr-pm", 1, "暂停／放弃", None, "当前结构厚度不满足本代项目目标", ["结构厚度"], "否决", "已确认", 0.91),
        ("obs-e-product", "package-e", "run-e", "customer-e", "tech-circular-polarizer", "platform-315-6k", "customer-project-e-optics", "function-product", "speaker-e-product", "evidence-e-product", 4, "内部评估", "主动推动方案比较", None, ["规格"], "推动", "已确认", 0.89),
        ("obs-e-quality", "package-e", "run-e", "customer-e", "tech-stw", "platform-315-6k", "customer-project-e-optics", "function-quality", "speaker-e-quality", "evidence-e-quality", 3, "样品／技术验证", None, "环境可靠性尚待验证", ["品质", "可靠性"], "把关", "已确认", 0.87),
    ]
    for spec in observation_specs:
        (
            observation_id,
            package_id,
            run_id,
            customer_id,
            technology_id,
            platform_id,
            customer_project_id,
            function_id,
            speaker_id,
            evidence_id,
            attitude,
            stage,
            support,
            resistance,
            bottlenecks,
            influence,
            status,
            confidence,
        ) = spec
        evidence_excerpt = next(item[5] for item in evidence_specs if item[0] == evidence_id)
        session.add(
            m.RawIntentObservation(
                id=observation_id,
                package_id=package_id,
                analysis_run_id=run_id,
                customer_id=customer_id,
                technology_id=technology_id,
                generic_platform_id=platform_id,
                customer_project_id=customer_project_id,
                function_id=function_id,
                speaker_id=speaker_id,
                evidence_id=evidence_id,
                raw_quote=evidence_excerpt,
                suggested_attitude=attitude,
                suggested_adoption_stage=stage,
                support_reason=support,
                resistance_reason=resistance,
                bottlenecks=bottlenecks,
                decision_influence=influence,
                confidence=confidence,
                status=status,
                uncertainty_reason="职能尚未完成身份校验" if function_id is None else None,
            )
        )
    session.flush()

    # A-side historical product status demonstrates append-only change from 3 to 5.
    historical = m.FormalIntentStatus(
        id="formal-a-product-v1",
        status_key="customer-a|tech-mini-led|platform-27-5k|customer-project-a-pro|function-product",
        customer_id="customer-a",
        technology_id="tech-mini-led",
        generic_platform_id="platform-27-5k",
        customer_project_id="customer-project-a-pro",
        function_id="function-product",
        attitude_level=3,
        is_supporter=False,
        is_resistance=False,
        conditions=["样机效果", "成本"],
        bottlenecks=["性能", "成本"],
        source_observation_ids=["obs-a-product-old"],
        effective_at=date(2026, 6, 25),
        confirmed_by="产品规划负责人",
        confirmed_at=_dt(26),
        change_note="首次沟通，保持有条件观望",
        version_no=1,
        is_current=False,
    )
    session.add(historical)
    session.flush()

    formal_specs = [
        ("formal-a-product-v2", "customer-a", "tech-mini-led", "platform-27-5k", "customer-project-a-pro", "function-product", 5, True, False, [], [], ["obs-a-product"], date(2026, 7, 18), "样机与路线图讨论后升级为主动推动", 2, "formal-a-product-v1"),
        ("formal-a-dev-v1", "customer-a", "tech-mini-led", "platform-27-5k", "customer-project-a-pro", "function-development", 4, False, False, ["完成兼容性验证"], ["验证资源"], ["obs-a-dev"], date(2026, 7, 18), "开发愿意安排样机验证", 1, None),
        ("formal-a-buy-v1", "customer-a", "tech-mini-led", "platform-27-5k", "customer-project-a-pro", "function-purchase", 2, False, True, ["成本增幅达到目标"], ["成本"], ["obs-a-buy"], date(2026, 7, 18), "采购侧当前暂不支持，与产品侧形成内部意见分化", 1, None),
        ("formal-a-quality-v1", "customer-a", "tech-mini-led", "platform-27-5k", "customer-project-a-pro", "function-quality", 3, False, False, ["补齐可靠性数据"], ["品质", "可靠性"], ["obs-a-quality"], date(2026, 7, 18), "品质侧等待验证数据", 1, None),
        ("formal-c-aglr-pm-v1", "customer-c", "tech-aglr", "platform-238", "customer-project-c-color", "function-pm", 1, False, True, [], ["结构厚度"], ["obs-c-aglr-pm"], date(2026, 7, 24), "PM明确阻断本代项目导入", 1, None),
        ("formal-e-product-v1", "customer-e", "tech-circular-polarizer", "platform-315-6k", "customer-project-e-optics", "function-product", 4, True, False, ["完成方案比较"], ["规格"], ["obs-e-product"], date(2026, 7, 29), "进入下一轮产品方案比较", 1, None),
        ("formal-e-quality-v1", "customer-e", "tech-stw", "platform-315-6k", "customer-project-e-optics", "function-quality", 3, False, False, ["完成环境可靠性验证"], ["品质", "可靠性"], ["obs-e-quality"], date(2026, 7, 29), "可靠性验证前保持观望", 1, None),
    ]
    for spec in formal_specs:
        (
            status_id,
            customer_id,
            technology_id,
            platform_id,
            project_id,
            function_id,
            attitude,
            supporter,
            resistance,
            conditions,
            bottlenecks,
            observation_ids,
            effective_at,
            note,
            version_no,
            previous_id,
        ) = spec
        item = m.FormalIntentStatus(
            id=status_id,
            status_key=f"{customer_id}|{technology_id}|{platform_id}|{project_id}|{function_id}",
            customer_id=customer_id,
            technology_id=technology_id,
            generic_platform_id=platform_id,
            customer_project_id=project_id,
            function_id=function_id,
            attitude_level=attitude,
            is_supporter=supporter,
            is_resistance=resistance,
            conditions=conditions,
            bottlenecks=bottlenecks,
            source_observation_ids=observation_ids,
            effective_at=effective_at,
            confirmed_by="产品规划负责人",
            confirmed_at=_dt(min(effective_at.day + 1, 30)),
            change_note=note,
            version_no=version_no,
            previous_version_id=previous_id,
            is_current=True,
        )
        session.add(item)
        session.flush()
        session.add(
            m.StatusVersion(
                id=f"version-{status_id}",
                entity_type="formal_intent_status",
                entity_id=status_id,
                version_no=version_no,
                snapshot={
                    "attitude_level": attitude,
                    "conditions": conditions,
                    "bottlenecks": bottlenecks,
                    "effective_at": effective_at.isoformat(),
                    "data_scope": "模拟数据",
                },
                changed_by="产品规划负责人",
                change_reason=note,
            )
        )
    session.flush()
    session.add(
        m.StatusVersion(
            id="version-formal-a-product-v1",
            entity_type="formal_intent_status",
            entity_id="formal-a-product-v1",
            version_no=1,
            snapshot={
                "attitude_level": 3,
                "conditions": ["样机效果", "成本"],
                "effective_at": "2026-06-25",
                "data_scope": "模拟数据",
            },
            changed_by="产品规划负责人",
            change_reason="首次沟通，保持有条件观望",
        )
    )

    historical_adoption = m.AdoptionStageState(
        id="adoption-a-mini-v1",
        status_key="customer-a|tech-mini-led|platform-27-5k|customer-project-a-pro",
        customer_id="customer-a",
        technology_id="tech-mini-led",
        generic_platform_id="platform-27-5k",
        customer_project_id="customer-project-a-pro",
        stage="初步了解",
        source_observation_ids=["obs-a-product-old"],
        effective_at=date(2026, 6, 25),
        confirmed_by="产品规划负责人",
        confirmed_at=_dt(26),
        change_note="首次技术沟通",
        version_no=1,
        is_current=False,
    )
    session.add(historical_adoption)
    session.flush()
    adoption_specs = [
        ("adoption-a-mini-v2", "customer-a", "tech-mini-led", "platform-27-5k", "customer-project-a-pro", "样品／技术验证", ["obs-a-product", "obs-a-dev"], date(2026, 7, 18), 2, "adoption-a-mini-v1"),
        ("adoption-c-aglr-v1", "customer-c", "tech-aglr", "platform-238", "customer-project-c-color", "暂停／放弃", ["obs-c-aglr-pm"], date(2026, 7, 24), 1, None),
        ("adoption-e-cp-v1", "customer-e", "tech-circular-polarizer", "platform-315-6k", "customer-project-e-optics", "内部评估", ["obs-e-product"], date(2026, 7, 29), 1, None),
        ("adoption-e-stw-v1", "customer-e", "tech-stw", "platform-315-6k", "customer-project-e-optics", "样品／技术验证", ["obs-e-quality"], date(2026, 7, 29), 1, None),
    ]
    for adoption_id, customer_id, technology_id, platform_id, project_id, stage, observation_ids, effective_at, version_no, previous_id in adoption_specs:
        adoption = m.AdoptionStageState(
            id=adoption_id,
            status_key=f"{customer_id}|{technology_id}|{platform_id}|{project_id}",
            customer_id=customer_id,
            technology_id=technology_id,
            generic_platform_id=platform_id,
            customer_project_id=project_id,
            stage=stage,
            source_observation_ids=observation_ids,
            effective_at=effective_at,
            confirmed_by="产品规划负责人",
            confirmed_at=_dt(min(effective_at.day + 1, 30)),
            change_note="基于已确认会议证据",
            version_no=version_no,
            previous_version_id=previous_id,
            is_current=True,
        )
        session.add(adoption)
        session.flush()
        session.add(
            m.StatusVersion(
                id=f"version-{adoption_id}",
                entity_type="adoption_stage",
                entity_id=adoption_id,
                version_no=version_no,
                snapshot={
                    "stage": stage,
                    "effective_at": effective_at.isoformat(),
                    "source_observation_ids": observation_ids,
                    "data_scope": "模拟数据",
                },
                changed_by="产品规划负责人",
                change_reason="基于已确认会议证据",
            )
        )
    session.add(
        m.StatusVersion(
            id="version-adoption-a-mini-v1",
            entity_type="adoption_stage",
            entity_id="adoption-a-mini-v1",
            version_no=1,
            snapshot={
                "stage": "初步了解",
                "effective_at": "2026-06-25",
                "source_observation_ids": ["obs-a-product-old"],
                "data_scope": "模拟数据",
            },
            changed_by="产品规划负责人",
            change_reason="首次技术沟通",
        )
    )

    session.commit()
