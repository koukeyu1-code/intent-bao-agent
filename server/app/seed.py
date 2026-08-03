from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models as m
from .config import Settings
from .taxonomy_excel import normalize_menu_value


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

# Eight display technologies.  Each one owns the same 3 x 3 platform menu
# (尺寸&分辨率 x 刷新率) so the cascade component can demonstrate the full
# technology -> 二级规格 -> 三级规格 -> 具体平台 hierarchy for every entry.
TECHNOLOGY_SPECS = [
    ("tech-mini-led-backlight", "MINI_LED_BACKLIGHT", "MiniLED背光", "背光技术",
     "高分区高亮度直下式背光，提升HDR画质与静态对比度，增强高端产品溢价能力"),
    ("tech-wide-gamut-color", "WIDE_GAMUT_COLOR", "高色域色彩", "色彩技术",
     "提升广色域覆盖与色准表现，满足专业设计与影音内容创作的色彩输出需求"),
    ("tech-low-reflection", "LOW_REFLECTION", "低反射光学", "光学技术",
     "提升强光/办公场景下的可视性与阅读舒适度"),
    ("tech-eye-care", "EYE_CARE_HEALTH", "护眼健康", "健康技术",
     "缓解长时间观看的视觉疲劳与不适，以及一切可以服务于人类健康改善的技术"),
    ("tech-pro-gaming", "PRO_GAMING", "极致电竞", "电竞技术",
     "聚焦电竞场景极致体验优化，涵盖超高刷新率、超低响应、动态清晰度、辅助功能等旗舰技术提升"),
    ("tech-refresh-boost", "REFRESH_BOOST", "刷新率提升", "产品功能",
     "在BOM成本基本持平的前提下，实现刷新率档位升级或者三模等变种，增强同价位产品竞争力"),
    ("tech-dual-tri-mode", "DUAL_TRI_MODE", "双模与三模", "产品功能",
     "分辨率、刷新率可切换，兼顾设计、影音、电竞多场景需求"),
    ("tech-other-innovation", "OTHER_INNOVATION", "其他创新", "创新技术",
     "差异化功能/形态/交互创新，打造产品独特卖点与市场区隔"),
]

# Level-2 (尺寸&分辨率) and level-3 (刷新率) values shared by every technology.
LEVEL2_VALUES = ["23.8FHD", "27QHD", "31.5UHD"]
LEVEL3_VALUES = ["180Hz", "240Hz", "300Hz"]

# Customer functions remain abstract dimensions (purchase/development/pm/
# product/quality).  Speaker display names use the realistic 代称 the product
# owner requested, each mapped to its function so the dashboards still aggregate
# by function while the evidence trail shows real-looking people.
FUNCTION_SPECS = [
    ("function-purchase", "PURCHASE", "采购", 10),
    ("function-development", "DEVELOPMENT", "开发", 20),
    ("function-pm", "PM", "PM", 30),
    ("function-product", "PRODUCT", "产品", 40),
    ("function-quality", "QUALITY", "品质", 50),
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


def _seed_platform_taxonomy(session: Session, tech_id: str) -> None:
    """Create the 3 x 3 platform menu tree for one technology.

    Mirrors the structure the Excel importer produces so the cascade selector
    shows 尺寸&分辨率 -> 刷新率 -> 具体平台 for the seeded demonstration data.
    """
    revision = 1
    defs: dict[int, m.PlatformMenuDefinition] = {}
    for level, name in ((2, "尺寸&分辨率"), (3, "刷新率")):
        definition = m.PlatformMenuDefinition(
            id=f"menu-def-{tech_id}-L{level}",
            technology_id=tech_id,
            level=level,
            name=name,
            sort_order=level,
            active=True,
            taxonomy_revision=revision,
        )
        session.add(definition)
        defs[level] = definition
    session.flush()

    values: dict[int, list[m.PlatformMenuValue]] = {2: [], 3: []}
    for index, raw in enumerate(LEVEL2_VALUES):
        value = m.PlatformMenuValue(
            id=f"menu-val-{tech_id}-L2-{index}",
            definition_id=defs[2].id,
            value=raw,
            normalized_value=normalize_menu_value(raw),
            sort_order=index,
            status="active",
            source="seed",
            created_by="系统初始化",
            taxonomy_revision=revision,
        )
        session.add(value)
        values[2].append(value)
    for index, raw in enumerate(LEVEL3_VALUES):
        value = m.PlatformMenuValue(
            id=f"menu-val-{tech_id}-L3-{index}",
            definition_id=defs[3].id,
            value=raw,
            normalized_value=normalize_menu_value(raw),
            sort_order=index,
            status="active",
            source="seed",
            created_by="系统初始化",
            taxonomy_revision=revision,
        )
        session.add(value)
        values[3].append(value)
    session.flush()

    for l2 in values[2]:
        for l3 in values[3]:
            display_name = f"{l2.value} {l3.value}"
            platform = m.GenericPlatform(
                id=f"platform-{tech_id}-{l2.normalized_value}-{l3.normalized_value}",
                code=f"{tech_id}_{l2.normalized_value}_{l3.normalized_value}",
                name=display_name,
                product_series="演示平台",
                active=True,
            )
            session.add(platform)
            session.flush()
            session.add(
                m.PlatformVariant(
                    id=f"variant-{tech_id}-{l2.normalized_value}-{l3.normalized_value}",
                    technology_id=tech_id,
                    level2_value_id=l2.id,
                    level3_value_id=l3.id,
                    generic_platform_id=platform.id,
                    display_name=display_name,
                    path_snapshot={
                        "technologyId": tech_id,
                        "level2": {"valueId": l2.id, "value": l2.value},
                        "level3": {"valueId": l3.id, "value": l3.value},
                        "displayName": display_name,
                    },
                    taxonomy_revision=revision,
                    mapping_status="mapped",
                    active=True,
                    created_by="系统初始化",
                )
            )
    session.flush()


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
        m.Customer(
            id=f"customer-{key.lower()}",
            code=key,
            name=f"客户{key}",
            display_name=f"客户{key}",
        )
        for key in "ABC"
    ]
    technologies = [
        m.Technology(
            id=tech_id,
            code=code,
            name=name,
            category=category,
            description=desc,
        )
        for tech_id, code, name, category, desc in TECHNOLOGY_SPECS
    ]
    functions = [
        m.CustomerFunction(
            id=function_id, code=code, name=name, sort_order=sort_order
        )
        for function_id, code, name, sort_order in FUNCTION_SPECS
    ]
    session.add(project)
    session.add_all(customers + technologies + functions)
    session.flush()

    # Build the 3 x 3 platform taxonomy tree for every technology so the
    # cascade selector is fully populated for demonstration.
    for tech_id, *_ in TECHNOLOGY_SPECS:
        _seed_platform_taxonomy(session, tech_id)

    # Customer projects anchor observations to a concrete customer platform.
    # Reuse the first technology's 27QHD/240Hz leaf as the demonstration
    # platform so the evidence trail resolves to a mapped leaf.  The normalized
    # value preserves the original letter case (only decimal trims change), so
    # the referenced IDs must match the taxonomy IDs exactly.
    platform_led_27_240 = "platform-tech-mini-led-backlight-27QHD-240Hz"
    platform_gaming_27_240 = "platform-tech-pro-gaming-27QHD-240Hz"
    platform_color_27_240 = "platform-tech-wide-gamut-color-27QHD-240Hz"
    platform_low_reflection_27_240 = "platform-tech-low-reflection-27QHD-240Hz"
    customer_projects = [
        m.CustomerProject(
            id="customer-project-a-led",
            customer_id="customer-a",
            generic_platform_id=platform_led_27_240,
            code="A_LED_5K",
            name="A-MiniLED高端项目",
        ),
        m.CustomerProject(
            id="customer-project-b-gaming",
            customer_id="customer-b",
            generic_platform_id=platform_gaming_27_240,
            code="B_GAMING",
            name="B-电竞旗舰项目",
        ),
        m.CustomerProject(
            id="customer-project-c-color",
            customer_id="customer-c",
            generic_platform_id=platform_color_27_240,
            code="C_COLOR",
            name="C-专业色彩项目",
        ),
    ]
    aliases = [
        m.Alias(
            id="alias-mini-backlight",
            entity_type="technology",
            entity_id="tech-mini-led-backlight",
            alias="MiniLED",
        ),
        m.Alias(
            id="alias-tri-mode",
            entity_type="technology",
            entity_id="tech-dual-tri-mode",
            alias="三模",
        ),
        m.Alias(
            id="alias-refresh",
            entity_type="technology",
            entity_id="tech-refresh-boost",
            alias="高刷",
        ),
    ]
    session.add_all(customer_projects + aliases)
    session.flush()

    package_specs = [
        (
            "package-a-led",
            "customer-a",
            "客户A MiniLED背光 27英寸方案评审",
            date(2026, 7, 18),
            "已确认",
            "完整",
        ),
        (
            "package-a-led-old",
            "customer-a",
            "客户A MiniLED背光早期需求沟通",
            date(2026, 6, 25),
            "已确认",
            "完整",
        ),
        (
            "package-b-gaming",
            "customer-b",
            "客户B 极致电竞 27英寸产品交流",
            date(2026, 7, 22),
            "待意向确认",
            "完整",
        ),
        (
            "package-c-color",
            "customer-c",
            "客户C 高色域色彩与低反射光学讨论",
            date(2026, 7, 24),
            "待身份校验",
            "资料不完整",
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

    demo_notes = {
        "package-a-led": (
            "客户A MiniLED背光 27英寸方案评审纪要（模拟）\n"
            "赵总(产品)：可以纳入路线图候选，先推进样机验证。\n"
            "王工(开发)：8月可安排兼容性验证。\n"
            "李总(采购)：需要把背光成本增幅控制在目标范围内。\n"
            "孙工(品质)：可靠性数据补齐后再进入正式评审。\n"
        ),
        "package-b-gaming": (
            "客户B 极致电竞 27英寸产品交流纪要（模拟）\n"
            "赵总(产品)：电竞高刷是差异化卖点，主动推动。\n"
            "王工(开发)：需要验证动态清晰度和响应延迟。\n"
            "李总(采购)：成本可控，配合评估。\n"
        ),
        "package-c-color": (
            "客户C 高色域色彩与低反射光学讨论纪要（模拟）\n"
            "产品侧希望提升色域覆盖，PM提出低反射结构厚度卡点。\n"
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

    # Attendees use the requested 代称, each bound to its customer function so
    # dashboards aggregate by function while the people look realistic.
    attendee_specs = [
        ("a-product", "package-a-led", "赵总", "市场/产品负责人", "产品中心", "function-product"),
        ("a-dev", "package-a-led", "王工", "开发经理", "开发部", "function-development"),
        ("a-buy", "package-a-led", "李总", "采购经理", "采购部", "function-purchase"),
        ("a-quality", "package-a-led", "孙工", "品质工程师", "品质部", "function-quality"),
        ("b-product", "package-b-gaming", "赵总", "市场/产品负责人", "产品中心", "function-product"),
        ("b-dev", "package-b-gaming", "王工", "开发工程师", "开发部", "function-development"),
        ("b-buy", "package-b-gaming", "李总", "采购经理", "采购部", "function-purchase"),
        ("c-unknown", "package-c-color", "王工", "待确认", "客户C", None),
        ("c-pm", "package-c-color", "吴总", "项目经理", "PM部", "function-pm"),
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
        ("run-a-led-old", "package-a-led-old", "已完成"),
        ("run-a-led", "package-a-led", "已完成"),
        ("run-b-gaming", "package-b-gaming", "已完成"),
        ("run-c-color", "package-c-color", "已完成"),
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
        ("speaker-a-product", "package-a-led", "Speaker A", "赵总", "市场/产品负责人", "产品中心", "function-product", 0.94, "已确认"),
        ("speaker-a-dev", "package-a-led", "Speaker B", "王工", "开发经理", "开发部", "function-development", 0.88, "已确认"),
        ("speaker-a-buy", "package-a-led", "Speaker C", "李总", "采购经理", "采购部", "function-purchase", 0.86, "已确认"),
        ("speaker-a-quality", "package-a-led", "Speaker D", "孙工", "品质工程师", "品质部", "function-quality", 0.82, "已确认"),
        ("speaker-b-product", "package-b-gaming", "Speaker A", "赵总", "市场/产品负责人", "产品中心", "function-product", 0.90, "已确认"),
        ("speaker-b-dev", "package-b-gaming", "Speaker B", "王工", "开发工程师", "开发部", "function-development", 0.81, "已确认"),
        ("speaker-b-buy", "package-b-gaming", "Speaker C", "李总", "采购经理", "采购部", "function-purchase", 0.79, "已确认"),
        ("speaker-c-unknown", "package-c-color", "Speaker A", "王工", "待确认", "客户C", None, 0.54, "待确认"),
        ("speaker-c-pm", "package-c-color", "Speaker B", "吴总", "项目经理", "PM部", "function-pm", 0.88, "已确认"),
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
        ("evidence-a-product-old", "package-a-led-old", None, "会议纪要", "第2节", "方向有兴趣，但需要先确认样机效果和整体成本。", None, 0.87),
        ("evidence-a-product", "package-a-led", "material-package-a-led", "会议纪要", "第2节", "如果样机验证通过，可以把MiniLED背光放进明年高端路线图。", None, 0.96),
        ("evidence-a-dev", "package-a-led", "material-package-a-led", "转写片段", "00:18:20–00:18:42", "开发侧建议先做27英寸样机，8月安排兼容性验证。", 1_100_000, 0.92),
        ("evidence-a-buy", "package-a-led", "material-package-a-led", "会议纪要", "第4节", "背光成本增幅需要控制在目标范围内，满足后再进入报价评估。", None, 0.91),
        ("evidence-a-quality", "package-a-led", "material-package-a-led", "会议纪要", "第5节", "需要补齐高温高湿和寿命数据，再进入品质评审。", None, 0.90),
        ("evidence-b-product", "package-b-gaming", "material-package-b-gaming", "会议纪要", "第2节", "电竞高刷对27英寸产品有差异化价值，可以放入路线图候选。", None, 0.93),
        ("evidence-b-dev", "package-b-gaming", "material-package-b-gaming", "转写片段", "00:26:05–00:26:36", "需要先验证动态清晰度和超低响应的实际表现。", 1_565_000, 0.87),
        ("evidence-b-buy", "package-b-gaming", "material-package-b-gaming", "会议纪要", "第4节", "成本基本可控，采购侧配合评估，但希望BOM持平。", None, 0.89),
        ("evidence-b-dev-frame", "package-b-gaming", "material-package-b-gaming", "录屏关键帧", "画面 00:26:30", "录屏画面显示动态清晰度测试仍有拖影，需要进一步调校响应。", None, 0.85),
        ("evidence-c-unknown", "package-c-color", None, "转写片段", "00:11:08–00:11:25", "这个色域方向可以再看看，但我还不能代表产品规划做结论。", 668_000, 0.58),
        ("evidence-c-low-reflection-pm", "package-c-color", None, "PDF页面", "第12页", "低反射光学在当前平台会增加结构厚度，PM明确提出本代项目不导入。", None, 0.91),
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
                page_no=12 if evidence_id == "evidence-c-low-reflection-pm" else None,
                confidence=confidence,
                playback_available=start_ms is not None,
            )
        )
    session.flush()

    # Observations bind customer + technology + platform leaf + function with a
    # five-level attitude and adoption-stage evidence.  The main line
    # (客户A · MiniLED背光) deliberately shows internal opinion divergence:
    # 产品 actively drives while 采购 withholds support.
    observation_specs = [
        # id, package, run, customer, technology, platform, project, function, speaker, evidence,
        # attitude, stage, support, resistance, bottlenecks, influence, status, confidence
        ("obs-a-product-old", "package-a-led-old", "run-a-led-old", "customer-a", "tech-mini-led-backlight", platform_led_27_240, "customer-project-a-led", "function-product", None, "evidence-a-product-old", 3, "初步了解", None, "需要确认样机效果和成本", ["性能", "成本"], "评估", "已被后续版本取代", 0.84),
        ("obs-a-product", "package-a-led", "run-a-led", "customer-a", "tech-mini-led-backlight", platform_led_27_240, "customer-project-a-led", "function-product", "speaker-a-product", "evidence-a-product", 5, "产品项目规划", "产品差异化价值明确，主动建议进入路线图", None, [], "推动", "已确认", 0.93),
        ("obs-a-dev", "package-a-led", "run-a-led", "customer-a", "tech-mini-led-backlight", platform_led_27_240, "customer-project-a-led", "function-development", "speaker-a-dev", "evidence-a-dev", 4, "样品／技术验证", "愿意安排样机与兼容性验证", None, ["验证资源"], "评估", "已确认", 0.90),
        ("obs-a-buy", "package-a-led", "run-a-led", "customer-a", "tech-mini-led-backlight", platform_led_27_240, "customer-project-a-led", "function-purchase", "speaker-a-buy", "evidence-a-buy", 2, "内部评估", None, "成本增幅仍需收敛，当前暂不支持导入", ["成本"], "把关", "已确认", 0.88),
        ("obs-a-quality", "package-a-led", "run-a-led", "customer-a", "tech-mini-led-backlight", platform_led_27_240, "customer-project-a-led", "function-quality", "speaker-a-quality", "evidence-a-quality", 3, "样品／技术验证", None, "可靠性数据尚不完整", ["品质", "可靠性"], "把关", "已确认", 0.86),
        ("obs-b-product", "package-b-gaming", "run-b-gaming", "customer-b", "tech-pro-gaming", platform_gaming_27_240, "customer-project-b-gaming", "function-product", "speaker-b-product", "evidence-b-product", 5, "产品项目规划", "电竞高刷是差异化卖点，主动推动", None, [], "推动", "待确认", 0.91),
        ("obs-b-dev", "package-b-gaming", "run-b-gaming", "customer-b", "tech-pro-gaming", platform_gaming_27_240, "customer-project-b-gaming", "function-development", "speaker-b-dev", "evidence-b-dev", 3, "样品／技术验证", None, "需要验证动态清晰度和响应延迟", ["性能", "验证资源"], "评估", "待确认", 0.84),
        ("obs-b-buy", "package-b-gaming", "run-b-gaming", "customer-b", "tech-pro-gaming", platform_gaming_27_240, "customer-project-b-gaming", "function-purchase", "speaker-b-buy", "evidence-b-buy", 4, "内部评估", "成本基本可控，配合评估", None, [], "评估", "待确认", 0.88),
        ("obs-b-quality", "package-b-gaming", "run-b-gaming", "customer-b", "tech-pro-gaming", platform_gaming_27_240, "customer-project-b-gaming", "function-quality", "speaker-b-dev", "evidence-b-dev-frame", 3, "样品／技术验证", None, "动态清晰度测试仍有拖影", ["动态清晰度"], "把关", "已确认", 0.82),
        ("obs-c-unknown", "package-c-color", "run-c-color", "customer-c", "tech-wide-gamut-color", platform_color_27_240, "customer-project-c-color", None, "speaker-c-unknown", "evidence-c-unknown", None, "初步了解", None, None, [], "未知", "AI草稿", 0.55),
        ("obs-c-low-reflection-pm", "package-c-color", "run-c-color", "customer-c", "tech-low-reflection", platform_low_reflection_27_240, "customer-project-c-color", "function-pm", "speaker-c-pm", "evidence-c-low-reflection-pm", 1, "暂停／放弃", None, "当前结构厚度不满足本代项目目标", ["结构厚度"], "否决", "已确认", 0.91),
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

    # Historical product status demonstrates append-only change from 3 to 5.
    historical = m.FormalIntentStatus(
        id="formal-a-product-v1",
        status_key=f"customer-a|tech-mini-led-backlight|{platform_led_27_240}|customer-project-a-led|function-product",
        customer_id="customer-a",
        technology_id="tech-mini-led-backlight",
        generic_platform_id=platform_led_27_240,
        customer_project_id="customer-project-a-led",
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
        ("formal-a-product-v2", "customer-a", "tech-mini-led-backlight", platform_led_27_240, "customer-project-a-led", "function-product", 5, True, False, [], [], ["obs-a-product"], date(2026, 7, 18), "样机与路线图讨论后升级为主动推动", 2, "formal-a-product-v1"),
        ("formal-a-dev-v1", "customer-a", "tech-mini-led-backlight", platform_led_27_240, "customer-project-a-led", "function-development", 4, False, False, ["完成兼容性验证"], ["验证资源"], ["obs-a-dev"], date(2026, 7, 18), "开发愿意安排样机验证", 1, None),
        ("formal-a-buy-v1", "customer-a", "tech-mini-led-backlight", platform_led_27_240, "customer-project-a-led", "function-purchase", 2, False, True, ["成本增幅达到目标"], ["成本"], ["obs-a-buy"], date(2026, 7, 18), "采购侧当前暂不支持，与产品侧形成内部意见分化", 1, None),
        ("formal-a-quality-v1", "customer-a", "tech-mini-led-backlight", platform_led_27_240, "customer-project-a-led", "function-quality", 3, False, False, ["补齐可靠性数据"], ["品质", "可靠性"], ["obs-a-quality"], date(2026, 7, 18), "品质侧等待验证数据", 1, None),
        ("formal-c-low-reflection-pm-v1", "customer-c", "tech-low-reflection", platform_low_reflection_27_240, "customer-project-c-color", "function-pm", 1, False, True, [], ["结构厚度"], ["obs-c-low-reflection-pm"], date(2026, 7, 24), "PM明确阻断本代项目导入", 1, None),
        ("formal-b-quality-v1", "customer-b", "tech-pro-gaming", platform_gaming_27_240, "customer-project-b-gaming", "function-quality", 3, False, False, ["完成动态清晰度调校"], ["动态清晰度"], ["obs-b-quality"], date(2026, 7, 22), "品质侧发现拖影，需进一步调校", 1, None),
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
        id="adoption-a-led-v1",
        status_key=f"customer-a|tech-mini-led-backlight|{platform_led_27_240}|customer-project-a-led",
        customer_id="customer-a",
        technology_id="tech-mini-led-backlight",
        generic_platform_id=platform_led_27_240,
        customer_project_id="customer-project-a-led",
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
        ("adoption-a-led-v2", "customer-a", "tech-mini-led-backlight", platform_led_27_240, "customer-project-a-led", "样品／技术验证", ["obs-a-product", "obs-a-dev"], date(2026, 7, 18), 2, "adoption-a-led-v1"),
        ("adoption-c-low-reflection-v1", "customer-c", "tech-low-reflection", platform_low_reflection_27_240, "customer-project-c-color", "暂停／放弃", ["obs-c-low-reflection-pm"], date(2026, 7, 24), 1, None),
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
            id="version-adoption-a-led-v1",
            entity_type="adoption_stage",
            entity_id="adoption-a-led-v1",
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
