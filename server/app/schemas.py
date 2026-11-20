from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)


DataScope = Literal["模拟数据", "授权脱敏数据", "企业真实敏感数据"]
AdoptionStage = Literal[
    "未明确／未进入",
    "初步了解",
    "内部评估",
    "样品／技术验证",
    "产品项目规划",
    "确认导入",
    "暂停／放弃",
]


class StrictRequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AttendeeInput(StrictRequestModel):
    display_name: str = Field(min_length=1, max_length=100)
    role: str | None = Field(default=None, max_length=100)
    organization: str | None = Field(default=None, max_length=200)
    function_id: str | None = None


class MeetingPackageCreate(StrictRequestModel):
    project_id: str = "project-mnt"
    customer_id: str
    title: str = Field(min_length=1, max_length=300)
    meeting_date: date
    # Deliberately required.  A silent "模拟数据" default can route real
    # customer material through a more permissive model policy.
    data_scope: DataScope
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    notes: str | None = Field(default=None, max_length=4000)
    pdf_exception_reason: str | None = Field(default=None, max_length=1000)
    attendees: list[AttendeeInput] = Field(default_factory=list)
    # 客户端幂等键：同一逻辑提交超时重试时复用，服务端按
    # (project_id, client_request_id) 去重并返回已创建的资料包。
    client_request_id: str | None = Field(default=None, max_length=64)


class MeetingPackageUpdate(StrictRequestModel):
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=300)
    meeting_date: date | None = None
    notes: str | None = Field(default=None, max_length=4000)
    pdf_exception_reason: str | None = Field(default=None, max_length=1000)
    retention_days: int | None = Field(default=None, ge=1, le=3650)


class AnalyzeRequest(StrictRequestModel):
    adapter: str | None = None
    force: bool = False


class SpeakerConfirmRequest(StrictRequestModel):
    version: int = Field(ge=1)
    display_name: str | None = Field(default=None, max_length=100)
    role: str | None = Field(default=None, max_length=100)
    organization: str | None = Field(default=None, max_length=200)
    function_id: str | None = None
    identity_granularity: Literal["完整", "角色", "职能", "待确认"] = "职能"
    actor: str = Field(default="本地产品规划负责人", max_length=100)

    @field_validator(
        "display_name", "role", "organization", "function_id", mode="before"
    )
    @classmethod
    def normalize_optional_identity_text(cls, value):
        if value is None:
            return None
        cleaned = " ".join(str(value).split())
        return cleaned or None

    @model_validator(mode="after")
    def validate_identity_granularity(self):
        required_fields = {
            "职能": (("function_id", "客户职能"),),
            "角色": (("role", "角色／职务"), ("function_id", "客户职能")),
            "完整": (
                ("display_name", "姓名／脱敏称呼"),
                ("role", "角色／职务"),
                ("organization", "组织"),
                ("function_id", "客户职能"),
            ),
        }
        missing = [
            label
            for field_name, label in required_fields.get(
                self.identity_granularity, ()
            )
            if not getattr(self, field_name)
        ]
        if missing:
            raise ValueError(
                f"{self.identity_granularity}身份仍缺少：" + "、".join(missing)
            )
        return self


class SpeakerMergeRequest(StrictRequestModel):
    package_id: str = Field(min_length=1, max_length=64)
    keep_speaker_id: str = Field(min_length=1, max_length=64)
    merge_speaker_ids: list[str] = Field(
        min_length=1, max_length=20, default_factory=list
    )
    actor: str = Field(default="本地产品规划负责人", max_length=100)

    @model_validator(mode="after")
    def validate_merge_targets(self):
        if self.keep_speaker_id in self.merge_speaker_ids:
            raise ValueError("保留说话人不能同时出现在合并列表中")
        return self


class SpeakerSplitRequest(StrictRequestModel):
    package_id: str = Field(min_length=1, max_length=64)
    segment_ids: list[str] = Field(
        min_length=1, max_length=500, default_factory=list
    )
    actor: str = Field(default="本地产品规划负责人", max_length=100)


class IntentConfirmRequest(StrictRequestModel):
    version: int = Field(ge=1)
    attitude_level: int | None = Field(
        default=None, description="1-5；null 明确表示未知"
    )
    adoption_stage: AdoptionStage | None = None
    is_supporter: bool = False
    is_resistance: bool = False
    conditions: list[str] = Field(default_factory=list)
    bottlenecks: list[str] = Field(default_factory=list)
    supporter_evidence_note: str | None = Field(
        default=None,
        max_length=1000,
        description="当原始证据未体现主动推动时，由确认人补充支持者判定依据",
    )
    note: str | None = Field(default=None, max_length=2000)
    actor: str = Field(default="本地产品规划负责人", max_length=100)

    @field_validator("attitude_level")
    @classmethod
    def validate_attitude(cls, value: int | None) -> int | None:
        if value is not None and value not in {1, 2, 3, 4, 5}:
            raise ValueError("意向档位必须是1-5，未知请传null")
        return value

    @model_validator(mode="after")
    def validate_role_consistency(self):
        if self.attitude_level is None:
            if self.is_supporter or self.is_resistance:
                raise ValueError("未知态度不得同时标记为支持者或阻力方")
            return self
        if self.is_supporter and self.attitude_level not in {4, 5}:
            raise ValueError("支持者必须同时具有4或5档态度")
        if self.is_resistance and self.attitude_level not in {1, 2}:
            raise ValueError("阻力方只能与1或2档态度同时确认")
        if self.is_supporter and self.is_resistance:
            raise ValueError("同一正式状态不能同时标记为支持者和阻力方")
        return self


class ObservationUpdateRequest(StrictRequestModel):
    version: int = Field(ge=1)
    status: Literal["AI草稿", "待确认", "退回补充"] | None = None
    function_id: str | None = None
    suggested_attitude: int | None = None
    suggested_adoption_stage: AdoptionStage | None = None
    support_reason: str | None = None
    resistance_reason: str | None = None
    bottlenecks: list[str] = Field(default_factory=list)
    uncertainty_reason: str | None = None
    highlight_spec: str | None = Field(default=None, max_length=500)

    @field_validator("suggested_attitude")
    @classmethod
    def validate_suggested_attitude(
        cls, value: int | None
    ) -> int | None:
        if value is not None and value not in {1, 2, 3, 4, 5}:
            raise ValueError("意向档位必须是1-5，未知请传null")
        return value


class PlatformDraftSelection(StrictRequestModel):
    """Reviewed technology/platform path saved before formal confirmation.

    Normal users may select an existing value or propose content under an
    existing administrator-defined menu.  They cannot supply menu names.
    """

    technology_id: str = Field(min_length=1, max_length=64)
    level2_value_id: str | None = Field(default=None, max_length=64)
    level2_new_value: str | None = Field(default=None, max_length=200)
    level3_value_id: str | None = Field(default=None, max_length=64)
    level3_new_value: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def validate_value_sources(self):
        if bool(self.level2_value_id) == bool(self.level2_new_value):
            raise ValueError("二级菜单必须且只能选择现有内容或新增一个内容")
        if bool(self.level3_value_id) == bool(self.level3_new_value):
            raise ValueError("三级菜单必须且只能选择现有内容或新增一个内容")
        for field_name in ("level2_new_value", "level3_new_value"):
            value = getattr(self, field_name)
            if value is not None:
                clean = " ".join(value.split())
                if not clean:
                    raise ValueError("新增菜单内容不能为空")
                setattr(self, field_name, clean)
        return self


class PlatformDraftRequest(PlatformDraftSelection):
    version: int = Field(ge=1)


class ObservationReviewDraftRequest(ObservationUpdateRequest):
    """Atomically save reviewed intent fields and an optional platform path."""

    platform: PlatformDraftSelection | None = None


class TaxonomyImportCommitRequest(StrictRequestModel):
    preview_token: str = Field(min_length=16, max_length=200)
    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ModelAdapterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    endpoint: str | None = None
    model: str | None = None
    api_key_env_name: str | None = None


ModelProvider = Literal["mock", "ollama", "openai-compatible"]
ModelConnectionStatus = Literal["unknown", "connected", "error"]


class ModelSettingsUpdateRequest(StrictRequestModel):
    provider: ModelProvider
    base_url: str = Field(min_length=1, max_length=500)
    model_name: str = Field(min_length=1, max_length=200)
    api_key: SecretStr | None = Field(
        default=None,
        description="只写字段；后端仅保存在当前进程内存，任何响应均不回显",
    )
    clear_api_key: bool = False
    asr_provider: Literal["mock", "llamacpp"] | None = None
    asr_base_url: str | None = Field(default=None, max_length=500)
    asr_model_name: str | None = Field(default=None, max_length=200)

    @field_validator("clear_api_key")
    @classmethod
    def validate_key_action(cls, value: bool, info: ValidationInfo) -> bool:
        if value and info.data.get("api_key") is not None:
            raise ValueError("不能同时设置和清除API Key")
        return value


class ModelSettingsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ModelProvider
    base_url: str
    model_name: str
    api_key_configured: bool
    asr: dict[str, Any] | None = None
    connection_status: ModelConnectionStatus = "unknown"
    last_checked_at: str | None = None
    last_error: str | None = None


class ModelConnectionRequest(StrictRequestModel):
    """Optional unsaved settings used by connection/model-list probes."""

    provider: ModelProvider | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=500)
    model_name: str | None = Field(default=None, min_length=1, max_length=200)
    api_key: SecretStr | None = Field(default=None)


class OllamaModelDescriptor(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    model: str | None = None
    size: int | None = None
    parameter_size: str | None = None
    quantization_level: str | None = None


class ModelConnectionTestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    message: str
    models: list[OllamaModelDescriptor] = Field(default_factory=list)
    latency_ms: int = Field(ge=0)
    checked_at: str


class ModelListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    models: list[OllamaModelDescriptor] = Field(default_factory=list)
    checked_at: str


class OllamaIntentObservation(BaseModel):
    """Strict AI output contract; all values remain proposals for human review."""

    model_config = ConfigDict(extra="forbid")

    technology_id: str | None = Field(default=None, max_length=64)
    generic_platform_id: str | None = Field(default=None, max_length=64)
    function_id: str | None = Field(default=None, max_length=64)
    speaker_name: str | None = Field(default=None, max_length=100)
    speaker_role: str | None = Field(default=None, max_length=100)
    speaker_organization: str | None = Field(default=None, max_length=200)
    quote: str = Field(min_length=1, max_length=4000)
    attitude_level: int | None = Field(default=None, ge=1, le=5)
    adoption_stage: AdoptionStage | None = None
    support_reason: str | None = Field(default=None, max_length=2000)
    resistance_reason: str | None = Field(default=None, max_length=2000)
    bottlenecks: list[str] = Field(default_factory=list, max_length=20)
    decision_influence: Literal["推动", "把关", "评估", "影响", "未知"] | None = None
    confidence: float = Field(ge=0, le=1)
    uncertainty_reason: str | None = Field(default=None, max_length=2000)

    @field_validator(
        "technology_id",
        "generic_platform_id",
        "function_id",
        "speaker_name",
        "speaker_role",
        "speaker_organization",
        mode="before",
    )
    @classmethod
    def normalize_empty_identifiers(cls, value):
        """Coerce blank identifiers to null.

        Local models frequently emit ``""`` when they cannot map a material
        to a master-data identifier.  Downstream persistence treats a null
        technology as an unresolved candidate instead of failing the whole
        analysis run, which matches the product rule "unknown is not neutral
        and never becomes a fabricated 1-5 attitude".
        """
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("bottlenecks")
    @classmethod
    def validate_bottlenecks(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for item in value:
            clean = item.strip()
            if not clean:
                continue
            if len(clean) > 200:
                raise ValueError("单个卡点不得超过200字")
            if clean not in normalized:
                normalized.append(clean)
        return normalized

    @model_validator(mode="after")
    def protect_unknown_attitude(self):
        if self.attitude_level is None and not (
            self.uncertainty_reason and self.uncertainty_reason.strip()
        ):
            raise ValueError("态度未知时必须说明不确定原因")
        return self


class OllamaStructuredOutput(BaseModel):
    """JSON Schema sent to Ollama and used to validate its message content."""

    model_config = ConfigDict(extra="forbid")

    observations: list[OllamaIntentObservation] = Field(
        default_factory=list, max_length=100
    )


class OllamaAnalysisResult(BaseModel):
    """Provider-neutral dictionary shape returned by real-model adapters."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["ollama", "openai-compatible"]
    model: str
    observations: list[OllamaIntentObservation] = Field(
        default_factory=list, max_length=100
    )
    usage: dict[str, Any] | None = None
    done_reason: str | None = None
    num_predict: int | None = None


class SpeakerIdentityRecommendation(BaseModel):
    """Strict AI output contract for speaker identity recommendations."""

    model_config = ConfigDict(extra="forbid")

    speaker_label: str = Field(min_length=1, max_length=64)
    display_name: str | None = Field(default=None, max_length=100)
    role: str | None = Field(default=None, max_length=100)
    organization: str | None = Field(default=None, max_length=200)
    function_id: str | None = Field(default=None, max_length=64)
    granularity: Literal["完整", "角色", "职能", "待确认"] = "待确认"
    confidence: float = Field(ge=0, le=1)
    reason: str | None = Field(default=None, max_length=2000)


class IdentityRecommendationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    speakers: list[SpeakerIdentityRecommendation] = Field(
        default_factory=list, max_length=100
    )


class MaterialAlignment(BaseModel):
    """Strict AI output contract for transcript-to-page alignment."""

    model_config = ConfigDict(extra="forbid")

    segment_id: str = Field(min_length=1, max_length=64)
    target_type: Literal["pdf_page", "key_frame"]
    target_id: str = Field(min_length=1, max_length=64)
    confidence: float = Field(ge=0, le=1)
    reason: str | None = Field(default=None, max_length=2000)


class MaterialAlignmentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alignments: list[MaterialAlignment] = Field(
        default_factory=list, max_length=200
    )


class GoldenIntentRecord(StrictRequestModel):
    quote: str = Field(min_length=1, max_length=4000)
    technology_id: str | None = Field(default=None, max_length=64)
    function_id: str | None = Field(default=None, max_length=64)
    attitude_level: int | None = Field(default=None, ge=1, le=5)
    adoption_stage: AdoptionStage | None = None


class EvalRunRequest(StrictRequestModel):
    package_id: str = Field(min_length=1, max_length=64)
    analysis_run_id: str | None = Field(default=None, min_length=1, max_length=64)
    golden: list[GoldenIntentRecord] = Field(
        min_length=1, max_length=500
    )
