from __future__ import annotations


ATTITUDE_LABELS: dict[int | None, str] = {
    5: "主动推动",
    4: "明确支持",
    3: "有条件／观望",
    2: "暂不支持",
    1: "明确反对／阻断",
    None: "未知",
}

ADOPTION_STAGES = [
    "未明确／未进入",
    "初步了解",
    "内部评估",
    "样品／技术验证",
    "产品项目规划",
    "确认导入",
    "暂停／放弃",
]

PACKAGE_STATUSES = [
    "草稿",
    "待预检",
    "待补资料／待标注",
    "待分析",
    "分析中",
    "待身份校验",
    "待意向确认",
    "已确认",
    "处理失败",
    "已取消",
]

TERMINAL_OBSERVATION_STATUSES = {"已确认", "无法判断", "已被后续版本取代"}


def formal_status_key(
    customer_id: str,
    technology_id: str,
    generic_platform_id: str | None,
    customer_project_id: str | None,
    function_id: str,
) -> str:
    return "|".join(
        [
            customer_id,
            technology_id,
            generic_platform_id or "-",
            customer_project_id or "-",
            function_id,
        ]
    )


def adoption_status_key(
    customer_id: str,
    technology_id: str,
    generic_platform_id: str | None,
    customer_project_id: str | None,
) -> str:
    return "|".join(
        [
            customer_id,
            technology_id,
            generic_platform_id or "-",
            customer_project_id or "-",
        ]
    )
