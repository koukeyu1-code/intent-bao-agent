"use client";

import {
  ApiOutlined,
  AppstoreOutlined,
  ApartmentOutlined,
  ArrowRightOutlined,
  AuditOutlined,
  CheckCircleFilled,
  CheckCircleOutlined,
  ClockCircleOutlined,
  CloudUploadOutlined,
  DatabaseOutlined,
  FileSearchOutlined,
  FileTextOutlined,
  FilterOutlined,
  HistoryOutlined,
  InboxOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlayCircleOutlined,
  PlusOutlined,
  RadarChartOutlined,
  SafetyCertificateOutlined,
  SearchOutlined,
  SettingOutlined,
  SoundOutlined,
  UserSwitchOutlined,
  WarningOutlined,
} from "@ant-design/icons";
import {
  App as AntApp,
  Badge,
  Button,
  Card,
  Checkbox,
  ConfigProvider,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Layout,
  Menu,
  Modal,
  Progress,
  Select,
  Space,
  Statistic,
  Table,
  Tabs,
  Tag,
  Timeline,
  Tooltip,
  Typography,
  Upload,
  message,
} from "antd";
import type { MenuProps, TableColumnsType, UploadProps } from "antd";
import type { UploadFile } from "antd";
import type { ECharts, EChartsOption } from "echarts";
import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  API_BASE,
  analyzeMeetingPackage,
  confirmSpeakerIdentity,
  createMeetingPackage,
  loadBootstrapDataset,
  materialTypeForFile,
  updateIntentSuggestion,
  updateIntentStatus,
  uploadMeetingMaterial,
} from "./api-client";
import {
  ADOPTION_STAGE_ORDER,
  computeOverviewMetrics,
  groupFormalIntentsByScope,
  latestFormalStatus,
} from "./intent-view-models";
import ModelSettingsPage from "./model-settings-page";
import { mockDataset } from "./mock-data";
import {
  adoptionStageColor,
  attitudeMeta,
  type AttitudeLevel,
  type DataSourceMode,
  type DictionaryCollection,
  type DictionaryItem,
  type Evidence,
  type FormalIntentStatus,
  type HistoryEvent,
  type IntentObservation,
  type IntentStatus,
  type MeetingPackage,
  type MvpDataset,
  type SpeakerSegment,
  type ViewKey,
} from "./types";

const { Sider, Content } = Layout;
const { Title, Paragraph } = Typography;
const FUNCTION_ORDER = ["产品", "PM", "开发", "采购", "品质"];
const ATTITUDE_LEVELS: AttitudeLevel[] = [5, 4, 3, 2, 1, null];
type ReloadDataset = () => Promise<MvpDataset>;
type NavigateTo = (key: ViewKey, packageId?: string) => void;
type OpenEvidence = (id: string, statusKey?: string) => void;

const compactDateTime = (value?: string) => {
  if (!value) return "暂无正式状态";
  const normalized = value.replace("T", " ");
  return normalized.length >= 16 ? normalized.slice(5, 16) : normalized;
};

const formatFileSize = (sizeBytes: number) =>
  sizeBytes >= 1024 * 1024
    ? `${(sizeBytes / 1024 / 1024).toFixed(1)} MB`
    : `${Math.max(1, Math.round(sizeBytes / 1024))} KB`;

const navItems: MenuProps["items"] = [
  { key: "overview", icon: <RadarChartOutlined />, label: "总览" },
  { key: "meetings", icon: <InboxOutlined />, label: "会议资料包" },
  { key: "speakers", icon: <UserSwitchOutlined />, label: "说话人校验" },
  { key: "insights", icon: <AuditOutlined />, label: "AI洞察确认" },
  { type: "divider" },
  { key: "technology", icon: <AppstoreOutlined />, label: "技术视角" },
  { key: "customer", icon: <ApartmentOutlined />, label: "客户视角" },
  { key: "history", icon: <HistoryOutlined />, label: "历史证据" },
  { key: "dictionary", icon: <DatabaseOutlined />, label: "基础字典" },
  { type: "divider" },
  { key: "settings", icon: <SettingOutlined />, label: "模型设置" },
];

const pageMeta: Record<
  ViewKey,
  { eyebrow: string; title: string; description: string }
> = {
  overview: {
    eyebrow: "CUSTOMER INTENT CONTROL ROOM",
    title: "让客户声音照进产品规划",
    description:
      "从会议证据出发，看清不同职能的支持、阻力、卡点与变化。",
  },
  meetings: {
    eyebrow: "MEETING PACKAGE",
    title: "会议资料包",
    description:
      "把录音、录屏、转写稿、人工纪要与PDF组织成一次可追溯的客户交流。",
  },
  speakers: {
    eyebrow: "SPEAKER VERIFICATION",
    title: "说话人校验",
    description:
      "AI先分离与推荐，人工再确认姓名、角色、组织和客户职能。",
  },
  insights: {
    eyebrow: "HUMAN-IN-THE-LOOP",
    title: "AI洞察确认",
    description:
      "逐条核对原文、证据和建议；正式结论始终由产品规划负责人确认。",
  },
  technology: {
    eyebrow: "TECHNOLOGY VIEW",
    title: "技术视角",
    description:
      "以技术为入口，横向比较客户、产品平台与客户职能的真实态度。",
  },
  customer: {
    eyebrow: "CUSTOMER VIEW",
    title: "客户视角",
    description:
      "以客户为入口，识别不同技术的内部支持者、阻力方和推进卡点。",
  },
  history: {
    eyebrow: "EVIDENCE & VERSION",
    title: "历史证据",
    description:
      "从最新状态回到每一次会议、每一段原话和每一次人工确认。",
  },
  dictionary: {
    eyebrow: "MASTER DATA",
    title: "基础字典",
    description:
      "统一技术、客户、平台、项目、职能与别名，减少抽取结果歧义。",
  },
  settings: {
    eyebrow: "MODEL SETTINGS",
    title: "模型设置",
    description:
      "连接本地Ollama或保留模拟适配器，测试服务并选择当前活动模型。",
  },
};

function AttitudeBadge({
  value,
  compact = false,
}: {
  value: AttitudeLevel;
  compact?: boolean;
}) {
  const meta = value === null ? attitudeMeta.unknown : attitudeMeta[value];
  return (
    <span
      className={`attitude-badge ${compact ? "attitude-badge--compact" : ""}`}
      style={{ color: meta.color, background: meta.background }}
    >
      {compact ? meta.shortLabel : meta.label}
    </span>
  );
}

function ConfidenceMark({ value }: { value: number }) {
  const percent = Math.round(value * 100);
  const tone =
    percent >= 90 ? "high" : percent >= 75 ? "medium" : "low";
  return (
    <Tooltip title="模型字段置信度，不代表客户意向强弱">
      <span className={`confidence confidence--${tone}`}>
        模型置信度 {percent}%
      </span>
    </Tooltip>
  );
}

function DataScopeBanner({ source }: { source: DataSourceMode }) {
  const loading = source === "loading";
  return (
    <div className="data-scope-banner" role="status">
      <div className="data-scope-banner__main">
        <span className="simulation-flag">
          {loading ? "数据加载中" : "模拟数据"}
        </span>
        <span>
          {loading
            ? "正在读取本地API；连接结果明确前不展示内置演示业务数据。"
            : "当前页面用于验证产品流程与交互，不代表真实客户结论或模型准确率。"}
        </span>
      </div>
      <Tooltip title={`接口地址：${API_BASE}`}>
        <span className="connection-state">
          <Badge status={source === "api" ? "success" : source === "loading" ? "processing" : "warning"} />
          {source === "api"
            ? "本地API已连接"
            : source === "loading"
              ? "正在连接本地API"
              : "API未连接 · 使用前端回退"}
        </span>
      </Tooltip>
    </div>
  );
}

function EChartPanel({
  option,
  height = 280,
  ariaLabel,
}: {
  option: EChartsOption;
  height?: number;
  ariaLabel: string;
}) {
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let chart: ECharts | undefined;
    let cancelled = false;
    const node = ref.current;
    if (!node) return;

    void import("echarts").then((echarts) => {
      if (cancelled || !ref.current) return;
      chart = echarts.init(ref.current);
      chart.setOption(option);
      const onResize = () => chart?.resize();
      window.addEventListener("resize", onResize);
      chart.on("finished", () => {
        ref.current?.setAttribute("data-chart-ready", "true");
      });
      (chart as ECharts & { __resizeHandler?: () => void }).__resizeHandler =
        onResize;
    });

    return () => {
      cancelled = true;
      const resizeHandler = (
        chart as (ECharts & { __resizeHandler?: () => void }) | undefined
      )?.__resizeHandler;
      if (resizeHandler) window.removeEventListener("resize", resizeHandler);
      chart?.dispose();
    };
  }, [option]);

  return (
    <div
      ref={ref}
      className="echart-panel"
      style={{ height }}
      role="img"
      aria-label={ariaLabel}
    />
  );
}

function SectionHeading({
  eyebrow,
  title,
  action,
}: {
  eyebrow?: string;
  title: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="section-heading">
      <div>
        {eyebrow ? <span className="section-eyebrow">{eyebrow}</span> : null}
        <Title level={3}>{title}</Title>
      </div>
      {action}
    </div>
  );
}

function StatusTag({ value }: { value: string }) {
  const colorMap: Record<string, string> = {
    已确认: "success",
    待意向确认: "processing",
    待确认: "processing",
    待身份校验: "gold",
    分析中: "blue",
    待补资料: "orange",
    处理失败: "error",
    AI草稿: "purple",
    退回补充: "warning",
    无法判断: "default",
  };
  return <Tag color={colorMap[value] ?? "default"}>{value}</Tag>;
}

function EvidenceDrawer({
  open,
  evidence,
  intent,
  formalIntent,
  history,
  onClose,
}: {
  open: boolean;
  evidence?: Evidence;
  intent?: IntentObservation;
  formalIntent?: FormalIntentStatus;
  history: HistoryEvent[];
  onClose: () => void;
}) {
  return (
    <Drawer
      open={open}
      onClose={onClose}
      width={560}
      title={
        <div className="drawer-title">
          <FileSearchOutlined />
          <span>证据下钻</span>
          <Tag color="cyan">模拟数据</Tag>
        </div>
      }
    >
      {!evidence ? (
        <Empty description="未找到证据" />
      ) : (
        <div className="evidence-drawer">
          <div className="source-label">原始事实</div>
          <blockquote>“{evidence.quote}”</blockquote>
          <Descriptions
            column={1}
            size="small"
            items={[
              {
                key: "meeting",
                label: "会议",
                children: evidence.meetingTitle,
              },
              {
                key: "speaker",
                label: "表述人",
                children: `${evidence.speaker} · ${evidence.speakerFunction}`,
              },
              {
                key: "location",
                label: "证据位置",
                children: (
                  <Space wrap>
                    <Tag icon={<SoundOutlined />}>{evidence.timecode}</Tag>
                    {evidence.pdfPage ? (
                      <Tag icon={<FileTextOutlined />}>
                        PDF 第 {evidence.pdfPage} 页
                      </Tag>
                    ) : null}
                    {evidence.frameLabel ? <Tag>{evidence.frameLabel}</Tag> : null}
                  </Space>
                ),
              },
              {
                key: "date",
                label: "会议日期",
                children: evidence.meetingDate,
              },
              {
                key: "source",
                label: "证据类型",
                children: evidence.sourceType,
              },
            ]}
          />

          <div className="source-label source-label--ai">AI建议</div>
          <Paragraph className="ai-reason">{evidence.aiReason}</Paragraph>
          {intent || formalIntent ? (
            <div className="evidence-result-grid">
              <div>
                <span>职能态度</span>
                <AttitudeBadge
                  value={formalIntent?.attitude ?? intent?.attitude ?? null}
                  compact
                />
              </div>
              <div>
                <span>采用阶段</span>
                <Tag
                  color={
                    adoptionStageColor[
                      formalIntent?.adoptionStage ??
                        intent?.adoptionStage ??
                        "未明确／未进入"
                    ]
                  }
                >
                  {formalIntent?.adoptionStage ?? intent?.adoptionStage}
                </Tag>
              </div>
              <div>
                <span>卡点</span>
                <strong>{formalIntent?.blocker ?? intent?.blocker}</strong>
              </div>
              <div>
                <span>当前状态</span>
                {formalIntent ? (
                  <Tag color="success">正式状态 V{formalIntent.version}</Tag>
                ) : intent ? (
                  <StatusTag value={intent.status} />
                ) : null}
              </div>
            </div>
          ) : null}

          <div className="source-label source-label--human">人工确认历史</div>
          {history.length ? (
            <Timeline
              items={history.map((item) => ({
                color: item.actor.includes("AI") ? "blue" : "green",
                children: (
                  <div className="timeline-entry">
                    <div>
                      <strong>{item.title}</strong>
                      <Tag>{item.version}</Tag>
                    </div>
                    <span>{item.date} · {item.actor}</span>
                    <p>{item.description}</p>
                  </div>
                ),
              }))}
            />
          ) : (
            <div className="empty-history">尚无正式状态变更记录</div>
          )}
        </div>
      )}
    </Drawer>
  );
}

function OverviewPage({
  data,
  onNavigate,
  onEvidence,
}: {
  data: MvpDataset;
  onNavigate: NavigateTo;
  onEvidence: OpenEvidence;
}) {
  const metrics = useMemo(() => computeOverviewMetrics(data), [data]);
  const {
    pendingCount,
    evidenceCount,
    confirmedCount,
    activeTechnologyCount,
    riskCount,
    stageCounts,
  } = metrics;

  const attitudeOption = useMemo<EChartsOption>(() => {
    const labels = ["主动推动", "明确支持", "有条件", "暂不支持", "反对／阻断", "未知"];
    const values = [5, 4, 3, 2, 1, null].map(
      (level) =>
        data.formalIntents.filter((item) => item.attitude === level).length,
    );
    const chartMax = Math.max(3, Math.max(...values) + 1);
    return {
      animationDuration: 700,
      grid: { left: 12, right: 28, top: 18, bottom: 6, containLabel: true },
      xAxis: {
        type: "value",
        max: chartMax,
        minInterval: 1,
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: "#eef1f5" } },
      },
      yAxis: {
        type: "category",
        data: labels,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: "#475467", fontSize: 12 },
      },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      series: [
        {
          type: "bar",
          data: values.map((value, index) => ({
            value,
            itemStyle: {
              color: ["#0c8066", "#2780b9", "#e5a423", "#df7422", "#c4493c", "#98a2b3"][index],
              borderRadius: [0, 4, 4, 0],
            },
          })),
          barWidth: 14,
          label: { show: true, position: "right", color: "#344054" },
        },
      ],
    };
  }, [data.formalIntents]);

  const stageOption = useMemo<EChartsOption>(() => {
    return {
      tooltip: { trigger: "item" },
      legend: {
        bottom: 0,
        left: "center",
        itemWidth: 9,
        itemHeight: 9,
        textStyle: { color: "#667085", fontSize: 11 },
      },
      series: [
        {
          type: "pie",
          radius: ["48%", "72%"],
          center: ["50%", "42%"],
          avoidLabelOverlap: true,
          label: { show: false },
          data: ADOPTION_STAGE_ORDER.map((stage, index) => ({
            name: stage,
            value: stageCounts[stage],
            itemStyle: {
              color: [
                "#98a2b3",
                "#92d6c5",
                "#6db7dc",
                "#8f91d8",
                "#335ea8",
                "#45a477",
                "#c4493c",
              ][index],
            },
          })),
        },
      ],
    };
  }, [stageCounts]);

  const latestIntents = data.formalIntents
    .slice()
    .sort((a, b) => b.confirmedAt.localeCompare(a.confirmedAt))
    .slice(0, 4);

  return (
    <div className="page-stack">
      <section className="hero-panel">
        <div className="hero-panel__copy">
          <div className="hero-kicker">
            <span className="hero-kicker__mark" />
            意向宝 · 客户意向智能体
          </div>
          <h1>让客户声音照进产品规划</h1>
          <p>
            把会议纪要、录音、录屏与PDF整理成
            <strong>“技术 × 客户 × 产品平台 × 客户职能”</strong>
            的证据化意向地图。
          </p>
          <Space wrap size={12} className="hero-actions">
            <Button
              type="primary"
              size="large"
              icon={<AuditOutlined />}
              onClick={() => onNavigate("insights")}
            >
              处理 {pendingCount} 条待确认洞察
            </Button>
            <Button
              size="large"
              icon={<RadarChartOutlined />}
              onClick={() => onNavigate("technology")}
            >
              查看意向地图
            </Button>
          </Space>
        </div>
        <div className="hero-panel__signal" aria-label="意向证据链概览">
          <div className="signal-core">
            <span>证据</span>
            <strong>{evidenceCount}</strong>
            <small>可下钻片段</small>
          </div>
          <div className="signal-node signal-node--one">
            <span>技术</span>
            <strong>{activeTechnologyCount}</strong>
          </div>
          <div className="signal-node signal-node--two">
            <span>客户</span>
            <strong>
              {
                data.dictionaries.customers.filter(
                  (item) => item.status === "启用",
                ).length
              }
            </strong>
          </div>
          <div className="signal-node signal-node--three">
            <span>职能</span>
            <strong>
              {
                data.dictionaries.functions.filter(
                  (item) => item.status === "启用",
                ).length
              }
            </strong>
          </div>
          <div className="signal-node signal-node--four">
            <span>正式状态</span>
            <strong>{confirmedCount}</strong>
          </div>
        </div>
      </section>

      <div className="metric-grid">
        <Card className="metric-card metric-card--blue">
          <Statistic
            title="待产品规划确认"
            value={pendingCount}
            suffix="条"
            prefix={<AuditOutlined />}
          />
          <button type="button" onClick={() => onNavigate("insights")}>
            进入确认台 <ArrowRightOutlined />
          </button>
        </Card>
        <Card className="metric-card metric-card--teal">
          <Statistic
            title="进行中资料包"
            value={data.meetings.filter((item) => item.progress < 100).length}
            suffix="个"
            prefix={<InboxOutlined />}
          />
          <button type="button" onClick={() => onNavigate("meetings")}>
            查看处理进度 <ArrowRightOutlined />
          </button>
        </Card>
        <Card className="metric-card metric-card--amber">
          <Statistic
            title="需关注的阻力／未知"
            value={riskCount}
            suffix="项"
            prefix={<WarningOutlined />}
          />
          <span>未知不会被算作中立或不支持</span>
        </Card>
        <Card className="metric-card metric-card--violet">
          <Statistic
            title="纳入分析的技术"
            value={activeTechnologyCount}
            suffix="项"
            prefix={<AppstoreOutlined />}
          />
          <span>Mini LED、三模、AGLR 等</span>
        </Card>
      </div>

      <div className="dashboard-grid">
        <Card className="content-card dashboard-grid__wide">
          <SectionHeading
            eyebrow="LATEST SIGNALS"
            title="最新客户意向"
            action={
              <Button type="link" onClick={() => onNavigate("technology")}>
                查看全部
              </Button>
            }
          />
          <div className="signal-list">
            {latestIntents.map((item) => (
              <button
                className="signal-row"
                type="button"
                key={item.id}
                disabled={!item.evidenceId}
                onClick={() => onEvidence(item.evidenceId, item.statusKey)}
                title={
                  item.evidenceId
                    ? "查看正式状态证据"
                    : "当前正式状态暂无可下钻证据"
                }
              >
                <div className="signal-row__identity">
                  <strong>{item.customer}</strong>
                  <span>{item.technology} · {item.platform}</span>
                </div>
                <div className="signal-row__function">
                  <span>{item.functionName}</span>
                  <small>正式状态 V{item.version}</small>
                </div>
                <AttitudeBadge value={item.attitude} compact />
                <Tag color={adoptionStageColor[item.adoptionStage]}>
                  {item.adoptionStage}
                </Tag>
                <div className="signal-row__blocker">
                  <span>当前卡点</span>
                  <strong>{item.blocker}</strong>
                </div>
                <ArrowRightOutlined />
              </button>
            ))}
          </div>
        </Card>

        <Card className="content-card">
          <SectionHeading eyebrow="ATTITUDE" title="职能态度分布" />
          <EChartPanel
            option={attitudeOption}
            height={260}
            ariaLabel="五档职能态度及未知分布"
          />
        </Card>

        <Card className="content-card dashboard-grid__wide">
          <SectionHeading
            eyebrow="PROCESSING"
            title="会议资料包进度"
            action={
              <Button type="link" onClick={() => onNavigate("meetings")}>
                管理资料包
              </Button>
            }
          />
          <div className="package-progress-list">
            {data.meetings.slice(0, 4).map((meeting) => (
              <div className="package-progress-item" key={meeting.id}>
                <div>
                  <strong>{meeting.title}</strong>
                  <span>{meeting.date} · {meeting.materials.join(" / ")}</span>
                </div>
                <StatusTag value={meeting.status} />
                <div className="package-progress-item__bar">
                  <Progress
                    percent={meeting.progress}
                    showInfo={false}
                    strokeColor={meeting.progress === 100 ? "#128269" : "#245fb3"}
                  />
                  <span>{meeting.currentStep}</span>
                </div>
              </div>
            ))}
          </div>
        </Card>

        <Card className="content-card">
          <SectionHeading eyebrow="ADOPTION" title="客户技术采用阶段" />
          <EChartPanel
            option={stageOption}
            height={260}
            ariaLabel="客户技术采用阶段分布"
          />
        </Card>
      </div>

      <Card className="evidence-principle-card">
        <div className="principle-icon"><SafetyCertificateOutlined /></div>
        <div>
          <strong>AI给出建议，业务负责人确认结论</strong>
          <p>
            五档态度、采用阶段、模型置信度各自独立；每一条正式状态都要能回到会议、日期与原始证据。
          </p>
        </div>
        <Tag>证据可追溯目标 100%</Tag>
      </Card>
    </div>
  );
}

function MeetingsPage({
  meetings,
  onNavigate,
  source,
  reloadDataset,
}: {
  meetings: MeetingPackage[];
  onNavigate: NavigateTo;
  source: DataSourceMode;
  reloadDataset: ReloadDataset;
}) {
  const [createOpen, setCreateOpen] = useState(false);
  const [detail, setDetail] = useState<MeetingPackage | undefined>();
  const [fileList, setFileList] = useState<UploadFile[]>([]);
  const [uploadStatuses, setUploadStatuses] = useState<
    Record<string, "待上传" | "上传中" | "已上传" | "失败">
  >({});
  const [submitting, setSubmitting] = useState(false);
  const [submitStage, setSubmitStage] = useState("");
  const [analysisBusyId, setAnalysisBusyId] = useState<string>();
  const [supplementBusyId, setSupplementBusyId] = useState<string>();
  const [form] = Form.useForm();

  const handleCreate = async (values: {
    title: string;
    customer: string;
    date: string;
    dataClass: MeetingPackage["dataClass"];
    attendeeName?: string;
    attendeeRole?: string;
    attendeeOrganization?: string;
    attendeeFunction?: string;
  }) => {
    if (!fileList.length) {
      message.error("请至少选择一份会议材料后再创建资料包");
      return;
    }
    setSubmitting(true);
    let createdId = "";
    try {
      setSubmitStage("正在创建资料包…");
      const created = await createMeetingPackage(values);
      createdId = created.id;

      for (let index = 0; index < fileList.length; index += 1) {
        const selected = fileList[index].originFileObj;
        if (!selected) {
          throw new Error(`无法读取文件：${fileList[index].name}`);
        }
        setUploadStatuses((current) => ({
          ...current,
          [fileList[index].uid]: "上传中",
        }));
        setSubmitStage(`正在上传材料 ${index + 1}/${fileList.length}…`);
        try {
          await uploadMeetingMaterial(created.id, selected as File);
          setUploadStatuses((current) => ({
            ...current,
            [fileList[index].uid]: "已上传",
          }));
        } catch (error) {
          setUploadStatuses((current) => ({
            ...current,
            [fileList[index].uid]: "失败",
          }));
          throw error;
        }
      }

      setSubmitStage("正在启动AI分析任务…");
      const analysis = await analyzeMeetingPackage(created.id);
      await reloadDataset();
      message.success(
        `${analysis.message} 资料包编号：${created.id}`,
        5,
      );
      setCreateOpen(false);
      form.resetFields();
      setFileList([]);
      setUploadStatuses({});
    } catch (error) {
      await reloadDataset().catch(() => undefined);
      const reason =
        error instanceof Error ? error.message : "本地API暂不可用";
      message.error(
        createdId
          ? `资料包 ${createdId} 已创建，但后续处理未完成：${reason}`
          : `创建失败：${reason}。请先启动本地服务后重试`,
        6,
      );
    } finally {
      setSubmitting(false);
      setSubmitStage("");
    }
  };

  const columns: TableColumnsType<MeetingPackage> = [
    {
      title: "会议资料包",
      dataIndex: "title",
      width: 290,
      render: (_, record) => (
        <button
          type="button"
          className="table-title-button"
          onClick={() => setDetail(record)}
        >
          <strong>{record.title}</strong>
          <span>{record.id}</span>
        </button>
      ),
    },
    {
      title: "客户／日期",
      width: 130,
      render: (_, record) => (
        <div className="table-stack">
          <strong>{record.customer}</strong>
          <span>{record.date}</span>
        </div>
      ),
    },
    {
      title: "材料",
      width: 200,
      render: (_, record) => (
        <Space size={[4, 4]} wrap>
          {record.materials.length
            ? record.materials.map((item) => <Tag key={item}>{item}</Tag>)
            : <span className="muted">尚未上传</span>}
        </Space>
      ),
    },
    {
      title: "处理状态",
      width: 150,
      render: (_, record) => (
        <div className="table-stack">
          <StatusTag value={record.status} />
          <Progress percent={record.progress} size="small" />
        </div>
      ),
    },
    {
      title: "当前步骤／风险",
      render: (_, record) => (
        <div className="table-stack">
          <strong>{record.currentStep}</strong>
          {record.warning ? (
            <span className="warning-copy">
              <WarningOutlined /> {record.warning}
            </span>
          ) : (
            <span>更新于 {record.updatedAt}</span>
          )}
        </div>
      ),
    },
    {
      title: "操作",
      fixed: "right",
      width: 108,
      render: (_, record) => (
        <Button
          type="link"
          onClick={() => {
            if (record.status === "待身份校验") {
              onNavigate("speakers", record.id);
            } else if (record.status === "待意向确认") {
              onNavigate("insights", record.id);
            } else {
              setDetail(record);
            }
          }}
        >
          {record.status === "待身份校验"
            ? "去校验"
            : record.status === "待意向确认"
              ? "去确认"
              : "查看"}
        </Button>
      ),
    },
  ];

  const uploadProps: UploadProps = {
    multiple: true,
    beforeUpload: () => false,
    fileList,
    onChange: ({ fileList: nextFileList }) => {
      setFileList(nextFileList);
      setUploadStatuses((current) => {
        const next = { ...current };
        nextFileList.forEach((item) => {
          next[item.uid] ??= "待上传";
        });
        Object.keys(next).forEach((uid) => {
          if (!nextFileList.some((item) => item.uid === uid)) delete next[uid];
        });
        return next;
      });
    },
    onRemove: (file) => {
      setFileList((current) => current.filter((item) => item.uid !== file.uid));
    },
    accept: ".mp3,.wav,.m4a,.mp4,.txt,.md,.docx,.srt,.vtt,.pdf",
    itemRender: (_originNode, file, _files, actions) => {
      const rawSize = file.size ?? file.originFileObj?.size ?? 0;
      const sizeLabel =
        rawSize >= 1024 * 1024
          ? `${(rawSize / 1024 / 1024).toFixed(1)} MB`
          : `${Math.max(1, Math.round(rawSize / 1024))} KB`;
      const status = uploadStatuses[file.uid] ?? "待上传";
      return (
        <div className="upload-file-row">
          <FileTextOutlined />
          <div>
            <strong>{file.name}</strong>
            <span>{materialTypeForFile(file.name)} · {sizeLabel}</span>
          </div>
          <Tag
            color={
              status === "已上传"
                ? "success"
                : status === "失败"
                  ? "error"
                  : status === "上传中"
                    ? "processing"
                    : "default"
            }
          >
            {status}
          </Tag>
          <Button
            type="text"
            size="small"
            disabled={submitting}
            onClick={actions.remove}
          >
            移除
          </Button>
        </div>
      );
    },
  };

  const triggerAnalysis = async (meetingId: string) => {
    setAnalysisBusyId(meetingId);
    try {
      const result = await analyzeMeetingPackage(meetingId, true);
      await reloadDataset();
      message.success(result.message);
      setDetail(undefined);
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : "本地API暂不可用";
      message.error(`分析任务启动失败：${reason}`);
    } finally {
      setAnalysisBusyId(undefined);
    }
  };

  const supplementMaterial = async (meetingId: string, file: File) => {
    setSupplementBusyId(meetingId);
    try {
      await uploadMeetingMaterial(meetingId, file);
      const refreshed = await reloadDataset();
      setDetail(refreshed.meetings.find((item) => item.id === meetingId));
      message.success(`${file.name} 已补充到资料包`);
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : "本地API暂不可用";
      message.error(`补充材料失败：${reason}`);
    } finally {
      setSupplementBusyId(undefined);
    }
  };

  return (
    <div className="page-stack">
      <div className="page-toolbar">
        <div>
          <Tag color="blue">{meetings.length} 个资料包</Tag>
          <span>单场材料共同形成一个可追溯分析单元</span>
        </div>
        <Button
          type="primary"
          icon={<PlusOutlined />}
          onClick={() => setCreateOpen(true)}
        >
          新建会议资料包
        </Button>
      </div>

      <div className="workflow-strip">
        {[
          ["01", "创建与上传", "录音／录屏／纪要／PDF"],
          ["02", "AI分阶段处理", "转写、说话人、资料对齐"],
          ["03", "人工校验", "身份与意向逐条确认"],
          ["04", "进入规划看板", "正式状态与历史版本"],
        ].map(([index, title, copy], position) => (
          <React.Fragment key={index}>
            <div className="workflow-step">
              <span>{index}</span>
              <div><strong>{title}</strong><small>{copy}</small></div>
            </div>
            {position < 3 ? <ArrowRightOutlined /> : null}
          </React.Fragment>
        ))}
      </div>

      <Card className="content-card table-card">
        <Table
          rowKey="id"
          columns={columns}
          dataSource={meetings}
          pagination={{ pageSize: 6, hideOnSinglePage: true }}
          scroll={{ x: 1080 }}
        />
      </Card>

      <div className="rule-grid">
        <Card className="rule-card">
          <FileTextOutlined />
          <div>
            <strong>PDF配套规则</strong>
            <p>纯录音或转写稿原则上同步上传会议PDF；无PDF需人工确认后继续。</p>
          </div>
        </Card>
        <Card className="rule-card">
          <CloudUploadOutlined />
          <div>
            <strong>首期容量建议</strong>
            <p>会议不超过120分钟，单个音视频不超过2GB，PDF不超过100页。</p>
          </div>
        </Card>
        <Card className="rule-card">
          <SafetyCertificateOutlined />
          <div>
            <strong>数据属性先行</strong>
            <p>模拟、授权脱敏、真实敏感三类数据决定可使用的模型与导出边界。</p>
          </div>
        </Card>
      </div>

      <Modal
        open={createOpen}
        title="创建会议资料包"
        okText={submitting ? submitStage : "创建并处理资料包"}
        cancelText="取消"
        width={680}
        confirmLoading={submitting}
        okButtonProps={{ disabled: !fileList.length || submitting }}
        closable={!submitting}
        maskClosable={!submitting}
        onCancel={() => {
          if (!submitting) setCreateOpen(false);
        }}
        onOk={() => form.submit()}
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={{ dataClass: "模拟数据", date: "2026-07-30" }}
          onFinish={(values) => void handleCreate(values)}
        >
          <div className="form-grid">
            <Form.Item
              name="title"
              label="会议名称"
              rules={[{ required: true, message: "请输入会议名称" }]}
            >
              <Input placeholder="例如：客户A 27英寸新技术规划交流" />
            </Form.Item>
            <Form.Item
              name="customer"
              label="客户"
              rules={[{ required: true, message: "请选择客户" }]}
            >
              <Select
                options={["客户A", "客户B", "客户C", "客户D", "客户E"].map(
                  (item) => ({ value: item, label: item }),
                )}
              />
            </Form.Item>
            <Form.Item
              name="date"
              label="会议日期"
              rules={[{ required: true }]}
            >
              <Input type="date" />
            </Form.Item>
            <Form.Item
              name="dataClass"
              label="数据属性"
              rules={[{ required: true }]}
            >
              <Select
                options={[
                  "模拟数据",
                  "授权脱敏数据",
                  "企业真实敏感数据",
                ].map((item) => ({ value: item, label: item }))}
              />
            </Form.Item>
          </div>
          <div className="form-subheading">
            <div>
              <strong>参会人信息</strong>
              <span>可选填一人，后续可继续补充与校验</span>
            </div>
          </div>
          <div className="form-grid">
            <Form.Item name="attendeeName" label="姓名／脱敏称呼">
              <Input placeholder="例如：赵总" />
            </Form.Item>
            <Form.Item name="attendeeRole" label="角色／职务">
              <Input placeholder="例如：产品规划负责人" />
            </Form.Item>
            <Form.Item name="attendeeOrganization" label="组织">
              <Input placeholder="例如：客户A 产品中心" />
            </Form.Item>
            <Form.Item name="attendeeFunction" label="客户职能">
              <Select
                allowClear
                placeholder="请选择"
                options={FUNCTION_ORDER.map((value) => ({
                  value,
                  label: value,
                }))}
              />
            </Form.Item>
          </div>
          <Form.Item
            label="会议材料"
            required
            help="使用服务端返回的真实资料包编号逐个上传；模型调用范围由后端按照数据属性和活动模型策略校验"
          >
            <Upload.Dragger {...uploadProps}>
              <p className="ant-upload-drag-icon"><InboxOutlined /></p>
              <p className="ant-upload-text">拖拽或点击选择会议材料</p>
              <p className="ant-upload-hint">
                支持录音、MP4录屏、转写稿、人工纪要和PDF
              </p>
            </Upload.Dragger>
          </Form.Item>
          <div className="modal-note">
            <SafetyCertificateOutlined />
            当前为{source === "api" ? "本地API连接" : "前端演示"}模式。企业真实敏感数据只能在本地或企业内网处理。
          </div>
        </Form>
      </Modal>

      <Drawer
        title="会议资料包详情"
        open={Boolean(detail)}
        onClose={() => setDetail(undefined)}
        width={520}
      >
        {detail ? (
          <div className="meeting-detail">
            <span className="simulation-flag">{detail.dataClass}</span>
            <Title level={3}>{detail.title}</Title>
            <Descriptions
              column={1}
              items={[
                { key: "id", label: "资料包编号", children: detail.id },
                { key: "customer", label: "客户", children: detail.customer },
                { key: "date", label: "会议日期", children: detail.date },
                { key: "duration", label: "时长", children: detail.duration },
                {
                  key: "materials",
                  label: "材料",
                  children: detail.materials.join("、") || "尚未上传",
                },
                {
                  key: "technology",
                  label: "技术候选",
                  children: detail.technologies.join("、") || "待解析",
                },
                {
                  key: "platform",
                  label: "平台候选",
                  children: detail.platforms.join("、") || "待解析",
                },
              ]}
            />
            <div className="material-detail-list">
              <div className="material-detail-list__head">
                <strong>材料预检</strong>
                <Tag
                  color={
                    detail.materialCompleteness === "完整"
                      ? "success"
                      : detail.materialCompleteness === "资料不完整"
                        ? "warning"
                        : "default"
                  }
                >
                  {detail.materialCompleteness ?? "待检查"}
                </Tag>
              </div>
              {detail.materialFiles?.length ? (
                detail.materialFiles.map((file) => (
                  <div className="material-detail-item" key={file.id}>
                    <FileTextOutlined />
                    <div>
                      <strong>{file.name}</strong>
                      <span>
                        {file.type} · {formatFileSize(file.sizeBytes)}
                      </span>
                    </div>
                    <Tag>{file.parseStatus}</Tag>
                  </div>
                ))
              ) : (
                <p>尚无服务端材料明细；请补充材料后刷新。</p>
              )}
              {detail.warning ? (
                <p className="warning-copy">
                  <WarningOutlined /> {detail.warning}
                </p>
              ) : null}
            </div>
            <div className="detail-progress">
              <strong>{detail.currentStep}</strong>
              <Progress percent={detail.progress} />
              <Upload
                accept=".mp3,.wav,.m4a,.mp4,.txt,.md,.docx,.srt,.vtt,.pdf"
                showUploadList={false}
                beforeUpload={(file) => {
                  void supplementMaterial(detail.id, file as File);
                  return Upload.LIST_IGNORE;
                }}
              >
                <Button
                  block
                  icon={<CloudUploadOutlined />}
                  loading={supplementBusyId === detail.id}
                >
                  补充会议材料
                </Button>
              </Upload>
              <Button
                type="primary"
                icon={<PlayCircleOutlined />}
                loading={analysisBusyId === detail.id}
                disabled={!detail.materials.length}
                onClick={() => void triggerAnalysis(detail.id)}
              >
                启动／重跑AI分析
              </Button>
              <p className="identity-boundary">
                后端将根据资料数据属性与当前活动模型决定是否允许处理；不满足安全策略时会明确拒绝，不会自动降级或外发。
              </p>
            </div>
          </div>
        ) : null}
      </Drawer>
    </div>
  );
}

function SpeakersPage({
  speakers,
  meetings,
  meetingId,
  setSpeakers,
  onNavigate,
  onMeetingChange,
  reloadDataset,
}: {
  speakers: SpeakerSegment[];
  meetings: MeetingPackage[];
  meetingId?: string;
  setSpeakers: React.Dispatch<React.SetStateAction<SpeakerSegment[]>>;
  onNavigate: NavigateTo;
  onMeetingChange: (packageId: string) => void;
  reloadDataset: ReloadDataset;
}) {
  const scopedSpeakers = useMemo(
    () => speakers.filter((item) => item.meetingId === meetingId),
    [meetingId, speakers],
  );
  const availableMeetings = useMemo(
    () =>
      meetings.filter((meeting) =>
        speakers.some((speaker) => speaker.meetingId === meeting.id),
      ),
    [meetings, speakers],
  );
  const [activeSpeakerId, setActiveSpeakerId] = useState<string>();
  const activeSpeaker =
    scopedSpeakers.find((item) => item.id === activeSpeakerId) ??
    scopedSpeakers.find((item) => !item.confirmed) ??
    scopedSpeakers[0];
  const activeMeeting = meetings.find((meeting) => meeting.id === meetingId);

  const updateField = (
    id: string,
    field: keyof SpeakerSegment,
    value: string,
  ) => {
    setSpeakers((current) =>
      current.map((item) => (item.id === id ? { ...item, [field]: value } : item)),
    );
  };

  const confirmIdentity = async (speaker: SpeakerSegment) => {
    try {
      await confirmSpeakerIdentity(speaker.id, speaker.version ?? 1, {
        alias: speaker.alias,
        role: speaker.role,
        organization: speaker.organization,
        functionName: speaker.functionName,
      });
      await reloadDataset();
      message.success(`${speaker.speakerCode} 身份已确认`);
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : "本地API暂不可用";
      message.error(`身份确认失败：${reason}。页面未伪造确认结果`);
    }
  };

  const pending = scopedSpeakers.filter((item) => !item.confirmed).length;

  return (
    <div className="page-stack">
      <div className="page-toolbar">
        <div>
          <Tag color={pending ? "gold" : "success"}>
            {pending ? `${pending} 个身份待校验` : "身份校验已完成"}
          </Tag>
          <span>
            {activeMeeting?.title ?? "请选择会议资料包"} · 本场会议内识别，不建立跨会议声纹库
          </span>
        </div>
        <Space wrap>
          <Select
            value={meetingId}
            placeholder="选择会议资料包"
            onChange={onMeetingChange}
            options={availableMeetings.map((meeting) => ({
              value: meeting.id,
              label: meeting.title,
            }))}
            style={{ minWidth: 240 }}
          />
          <Button
            type="primary"
            disabled={!meetingId || pending > 0 || !scopedSpeakers.length}
            onClick={() => onNavigate("insights", meetingId)}
          >
            进入洞察确认 <ArrowRightOutlined />
          </Button>
        </Space>
      </div>

      <div className="speaker-workbench">
        <Card className="content-card speaker-list-panel">
          <SectionHeading eyebrow="DIARIZATION" title="说话人片段" />
          <div className="speaker-list">
            {scopedSpeakers.map((speaker) => (
              <button
                type="button"
                key={speaker.id}
                className={`speaker-list-item ${
                  activeSpeaker?.id === speaker.id ? "is-active" : ""
                }`}
                onClick={() => setActiveSpeakerId(speaker.id)}
              >
                <div className="speaker-avatar">
                  {speaker.speakerCode.replace("Speaker ", "")}
                </div>
                <div>
                  <strong>{speaker.speakerCode}</strong>
                  <span>{speaker.timeRange}</span>
                </div>
                {speaker.confirmed ? (
                  <CheckCircleFilled className="speaker-confirmed" />
                ) : (
                  <Badge status="warning" />
                )}
              </button>
            ))}
            {!scopedSpeakers.length ? (
              <Empty description="当前资料包暂无说话人片段" />
            ) : null}
          </div>
        </Card>

        <Card className="content-card transcript-panel">
          {activeSpeaker ? (
            <>
              <div className="audio-strip">
                <Button shape="circle" icon={<PlayCircleOutlined />} />
                <div className="audio-wave" aria-label="模拟音频波形">
                  {Array.from({ length: 42 }, (_, index) => (
                    <i
                      key={index}
                      style={{ height: `${8 + ((index * 13) % 26)}px` }}
                    />
                  ))}
                </div>
                <span>{activeSpeaker.timeRange.split("–")[0]}</span>
              </div>
              <div className="transcript-quote">
                <span>转写片段</span>
                <blockquote>“{activeSpeaker.excerpt}”</blockquote>
              </div>
              <div className="speaker-basis">
                <ApiOutlined />
                <div>
                  <strong>AI推荐依据</strong>
                  <p>{activeSpeaker.recommendationBasis}</p>
                </div>
                <ConfidenceMark value={activeSpeaker.confidence} />
              </div>
            </>
          ) : (
            <Empty description="暂无说话人片段" />
          )}
        </Card>

        <Card className="content-card identity-panel">
          {activeSpeaker ? (
            <>
              <SectionHeading
                eyebrow="HUMAN CHECK"
                title="身份与职能"
                action={
                  activeSpeaker.confirmed ? (
                    <Tag icon={<CheckCircleOutlined />} color="success">
                      已人工确认
                    </Tag>
                  ) : null
                }
              />
              <div className="identity-form">
                <label>
                  姓名／脱敏称呼
                  <Input
                    value={activeSpeaker.alias}
                    onChange={(event) =>
                      updateField(activeSpeaker.id, "alias", event.target.value)
                    }
                  />
                </label>
                <label>
                  角色／职务
                  <Input
                    value={activeSpeaker.role}
                    onChange={(event) =>
                      updateField(activeSpeaker.id, "role", event.target.value)
                    }
                  />
                </label>
                <label>
                  组织
                  <Input
                    value={activeSpeaker.organization}
                    onChange={(event) =>
                      updateField(
                        activeSpeaker.id,
                        "organization",
                        event.target.value,
                      )
                    }
                  />
                </label>
                <label>
                  客户职能
                  <Select
                    value={activeSpeaker.functionName}
                    onChange={(value) =>
                      updateField(activeSpeaker.id, "functionName", value)
                    }
                    options={FUNCTION_ORDER.map((value) => ({ value, label: value }))}
                  />
                </label>
              </div>
              <Checkbox className="identity-checkbox">
                已核对参会名单、称呼与发言内容
              </Checkbox>
              <Button
                type="primary"
                block
                size="large"
                icon={<CheckCircleOutlined />}
                disabled={activeSpeaker.confirmed}
                onClick={() => void confirmIdentity(activeSpeaker)}
              >
                {activeSpeaker.confirmed ? "身份已确认" : "确认本场身份"}
              </Button>
              <p className="identity-boundary">
                职能无法确认的片段可以保留，但不会进入正式职能意向统计。
              </p>
            </>
          ) : null}
        </Card>
      </div>
    </div>
  );
}

function InsightsPage({
  intents,
  evidence,
  meetingId,
  onEvidence,
  reloadDataset,
}: {
  intents: IntentObservation[];
  evidence: Evidence[];
  meetingId?: string;
  onEvidence: OpenEvidence;
  reloadDataset: ReloadDataset;
}) {
  const [selectedRowKeys, setSelectedRowKeys] = useState<React.Key[]>([]);
  const [statusFilter, setStatusFilter] = useState<"全部" | IntentStatus>("全部");
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<IntentObservation | undefined>();
  const [savingEdit, setSavingEdit] = useState(false);
  const meetingByEvidence = useMemo(
    () => new Map(evidence.map((item) => [item.id, item.meetingId])),
    [evidence],
  );
  const scopedIntents = useMemo(
    () =>
      meetingId
        ? intents.filter(
            (item) =>
              (item.meetingId ?? meetingByEvidence.get(item.evidenceId)) ===
              meetingId,
          )
        : intents,
    [intents, meetingByEvidence, meetingId],
  );

  const filtered = scopedIntents.filter((item) => {
    const matchesStatus =
      statusFilter === "全部" ? true : item.status === statusFilter;
    const haystack = [
      item.customer,
      item.technology,
      item.platform,
      item.functionName,
      item.blocker,
    ]
      .join(" ")
      .toLowerCase();
    return matchesStatus && haystack.includes(query.toLowerCase());
  });

  const applyAction = useCallback(
    async (
      ids: string[],
      action: "confirm" | "return" | "unknown",
    ) => {
      if (action === "confirm") {
        const blocked = scopedIntents.filter(
          (item) =>
            ids.includes(item.id) &&
            (item.conflict ||
              item.confidence < 0.75 ||
              item.person.includes("待确认") ||
              item.attitude === null),
        );
        if (blocked.length) {
          message.error(
            `${blocked.length} 条结果存在身份、冲突、未知或低置信度风险，需逐条处理`,
          );
          return;
        }
      }

      try {
        const persistenceResults = await Promise.allSettled(
          scopedIntents
            .filter((item) => ids.includes(item.id))
            .map((item) => updateIntentStatus(item, action)),
        );
        const failedWrites = persistenceResults.filter(
          (result) => result.status === "rejected",
        ).length;
        const successCount = ids.length - failedWrites;
        await reloadDataset();
        setSelectedRowKeys([]);
        const actionMessage =
          action === "confirm"
            ? `已确认 ${successCount} 条意向`
            : action === "return"
              ? `已为 ${successCount} 条结果记录退回补充要求`
              : `已将 ${successCount} 条标记为无法判断`;
        if (!successCount) {
          message.error("操作未写入服务端，已恢复服务端最新数据");
        } else if (failedWrites) {
          message.warning(
            `${actionMessage}；${failedWrites} 条写入失败，已按服务端最新数据刷新`,
          );
        } else {
          message.success(actionMessage);
        }
      } catch (error) {
        const reason =
          error instanceof Error ? error.message : "本地API暂不可用";
        message.error(`操作后刷新失败：${reason}。请重新加载页面核对`);
      }
    },
    [reloadDataset, scopedIntents],
  );

  const saveEditing = async () => {
    if (!editing) return;
    setSavingEdit(true);
    try {
      const expectedStage = editing.adoptionStage;
      await updateIntentSuggestion(editing);
      const refreshed = await reloadDataset();
      const persisted = refreshed.intents.find(
        (item) => item.id === editing.id,
      );
      if (!persisted || persisted.adoptionStage !== expectedStage) {
        throw new Error(
          `采用阶段保存校验失败：期望“${expectedStage}”，刷新后为“${
            persisted?.adoptionStage ?? "未找到该观察"
          }”`,
        );
      }
      setEditing(undefined);
      message.success("修改已写入服务端并校验，等待正式确认");
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : "本地API暂不可用";
      message.error(`修改失败：${reason}。页面未伪造保存结果`);
    } finally {
      setSavingEdit(false);
    }
  };

  const columns: TableColumnsType<IntentObservation> = [
    {
      title: "对象范围",
      width: 220,
      render: (_, item) => (
        <div className="table-stack">
          <strong>{item.customer} · {item.technology}</strong>
          <span>{item.platform}</span>
          <small>{item.customerProject}</small>
        </div>
      ),
    },
    {
      title: "职能／人员",
      width: 118,
      render: (_, item) => (
        <div className="table-stack">
          <strong>{item.functionName}</strong>
          <span>{item.person}</span>
        </div>
      ),
    },
    {
      title: "AI态度建议",
      width: 142,
      render: (_, item) => (
        <div className="table-stack">
          <AttitudeBadge value={item.attitude} compact />
          <ConfidenceMark value={item.confidence} />
        </div>
      ),
    },
    {
      title: "采用阶段",
      width: 128,
      render: (_, item) => (
        <Tag color={adoptionStageColor[item.adoptionStage]}>
          {item.adoptionStage}
        </Tag>
      ),
    },
    {
      title: "支持／卡点",
      render: (_, item) => (
        <div className="reason-cell">
          <span><CheckCircleOutlined /> {item.supportReason}</span>
          <strong><WarningOutlined /> {item.blocker}</strong>
          {item.conflict ? <Tag color="red">历史／内部意见冲突</Tag> : null}
        </div>
      ),
    },
    {
      title: "确认状态",
      width: 104,
      render: (_, item) => <StatusTag value={item.status} />,
    },
    {
      title: "操作",
      fixed: "right",
      width: 178,
      render: (_, item) => (
        <Space size={2}>
          <Button
            type="link"
            disabled={!item.evidenceId}
            onClick={() => onEvidence(item.evidenceId)}
          >
            证据
          </Button>
          <Button type="link" onClick={() => setEditing(item)}>
            修改
          </Button>
          <Button
            type="link"
            disabled={item.status === "已确认"}
            onClick={() => void applyAction([item.id], "confirm")}
          >
            确认
          </Button>
        </Space>
      ),
    },
  ];

  return (
    <div className="page-stack">
      <div className="confirmation-principle">
        <SafetyCertificateOutlined />
        <div>
          <strong>当前显示的是AI建议，不是正式结论</strong>
          <span>
            产品规划负责人确认后才进入看板；未知与证据不足不会被自动归为中立或不支持。
          </span>
        </div>
        {meetingId ? <Tag color="blue">当前资料包：{meetingId}</Tag> : null}
      </div>

      <Card className="content-card table-card">
        <div className="table-toolbar">
          <Space wrap>
            <Input
              prefix={<SearchOutlined />}
              placeholder="搜索客户、技术、平台或卡点"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              allowClear
              className="search-input"
            />
            <Select
              value={statusFilter}
              onChange={setStatusFilter}
              options={["全部", "AI草稿", "待确认", "已确认", "退回补充", "无法判断"].map(
                (value) => ({ value, label: value }),
              )}
              prefix={<FilterOutlined />}
              className="status-select"
            />
          </Space>
          <Space wrap>
            <span className="selected-count">已选 {selectedRowKeys.length} 条</span>
            <Button
              disabled={!selectedRowKeys.length}
              onClick={() =>
                void applyAction(
                  selectedRowKeys.map(String),
                  "return",
                )
              }
            >
              退回补充
            </Button>
            <Button
              disabled={!selectedRowKeys.length}
              onClick={() =>
                void applyAction(
                  selectedRowKeys.map(String),
                  "unknown",
                )
              }
            >
              无法判断
            </Button>
            <Button
              type="primary"
              disabled={!selectedRowKeys.length}
              icon={<CheckCircleOutlined />}
              onClick={() =>
                void applyAction(
                  selectedRowKeys.map(String),
                  "confirm",
                )
              }
            >
              批量确认
            </Button>
          </Space>
        </div>
        <Table
          rowKey="id"
          rowSelection={{
            selectedRowKeys,
            onChange: setSelectedRowKeys,
            getCheckboxProps: (record) => ({
              disabled: record.status === "已确认",
            }),
          }}
          columns={columns}
          dataSource={filtered}
          pagination={{ pageSize: 7 }}
          scroll={{ x: 1180 }}
        />
      </Card>

      <div className="legend-panel">
        <strong>五档态度</strong>
        {[5, 4, 3, 2, 1, null].map((level) => (
          <AttitudeBadge key={level ?? "unknown"} value={level as AttitudeLevel} compact />
        ))}
        <span className="legend-divider" />
        <strong>采用阶段独立记录</strong>
        <span>态度回答“谁支持”，阶段回答“推进到哪一步”</span>
      </div>

      <Modal
        open={Boolean(editing)}
        title="修改AI建议"
        okText="保存为待确认"
        cancelText="取消"
        confirmLoading={savingEdit}
        onCancel={() => setEditing(undefined)}
        onOk={() => void saveEditing()}
      >
        {editing ? (
          <div className="edit-intent-form">
            <label>
              职能态度
              <Select
                value={editing.attitude ?? "unknown"}
                onChange={(value) =>
                  setEditing({
                    ...editing,
                    attitude: value === "unknown" ? null : Number(value) as AttitudeLevel,
                  })
                }
                options={[
                  ...[5, 4, 3, 2, 1].map((value) => ({
                    value,
                    label: attitudeMeta[value].shortLabel,
                  })),
                  { value: "unknown", label: "— · 未知" },
                ]}
              />
            </label>
            <label>
              客户技术采用阶段
              <Select
                value={editing.adoptionStage}
                onChange={(value) =>
                  setEditing({ ...editing, adoptionStage: value })
                }
                options={Object.keys(adoptionStageColor).map((value) => ({
                  value,
                  label: value,
                }))}
              />
            </label>
            <label>
              当前卡点
              <Input.TextArea
                rows={3}
                value={editing.blocker}
                onChange={(event) =>
                  setEditing({ ...editing, blocker: event.target.value })
                }
              />
            </label>
          </div>
        ) : null}
      </Modal>
    </div>
  );
}

function TechnologyPage({
  intents,
  dictionaries,
  onEvidence,
}: {
  intents: FormalIntentStatus[];
  dictionaries: DictionaryCollection;
  onEvidence: OpenEvidence;
}) {
  const technologies = useMemo(() => {
    const active = dictionaries.technologies
      .filter((item) => item.status === "启用")
      .map((item) => item.name);
    return active.length
      ? active
      : Array.from(new Set(intents.map((item) => item.technology)));
  }, [dictionaries.technologies, intents]);
  const customers = useMemo(() => {
    const active = dictionaries.customers
      .filter((item) => item.status === "启用")
      .map((item) => item.name);
    return active.length
      ? active
      : Array.from(new Set(intents.map((item) => item.customer)));
  }, [dictionaries.customers, intents]);
  const [requestedTechnology, setRequestedTechnology] = useState("Mini LED");
  const technology = technologies.includes(requestedTechnology)
    ? requestedTechnology
    : technologies[0] ?? "";
  const scoped = useMemo(
    () => intents.filter((item) => item.technology === technology),
    [intents, technology],
  );
  const scopeRows = useMemo(
    () =>
      technology
        ? groupFormalIntentsByScope(intents, {
            technology,
            customerOrder: customers,
            includeEmptyCustomers: true,
          })
        : [],
    [customers, intents, technology],
  );

  const overviewOption = useMemo<EChartsOption>(
    () => ({
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      legend: {
        type: "scroll",
        bottom: 0,
        textStyle: { color: "#475467" },
      },
      grid: {
        left: 34,
        right: 18,
        top: 18,
        bottom: 62,
        containLabel: true,
      },
      xAxis: {
        type: "category",
        data: customers,
        axisTick: { show: false },
        axisLine: { lineStyle: { color: "#dbe5ef" } },
        axisLabel: { color: "#475467" },
      },
      yAxis: {
        type: "value",
        name: "职能状态数",
        minInterval: 1,
        axisLabel: { color: "#667085" },
        splitLine: { lineStyle: { color: "#edf2f7" } },
      },
      series: ATTITUDE_LEVELS.map((level) => {
        const meta = attitudeMeta[level ?? "unknown"];
        return {
          name: meta.label,
          type: "bar",
          stack: "function-status",
          barMaxWidth: 34,
          itemStyle: { color: meta.color },
          emphasis: { focus: "series" },
          data: customers.map(
            (customer) =>
              scoped.filter(
                (item) =>
                  item.customer === customer && item.attitude === level,
              ).length,
          ),
        };
      }),
    }),
    [customers, scoped],
  );

  const actions = useMemo(
    () =>
      scopeRows.map((row) => {
        const resistance = row.items.find((item) => item.isResistance);
        const missingFunctions = FUNCTION_ORDER.filter(
          (functionName) => !row.byFunction.has(functionName),
        );
        const latest = latestFormalStatus(row.items);
        if (!row.items.length) {
          return {
            key: row.key,
            priority: "补证据",
            scope: row.customer,
            action: `当前没有${technology}正式职能状态，下一次会议列入访谈清单`,
            owner: "会议发起人",
          };
        }
        if (resistance) {
          return {
            key: row.key,
            priority: "优先",
            scope: `${row.customer} · ${row.platform} · ${row.customerProject}`,
            action: `${resistance.functionName}：${resistance.blocker}`,
            owner: "产品企划＋对应职能",
          };
        }
        if (missingFunctions.length) {
          return {
            key: row.key,
            priority: "跟进",
            scope: `${row.customer} · ${row.platform} · ${row.customerProject}`,
            action: `补齐${missingFunctions.join("、")}职能的正式判断`,
            owner: "产品企划",
          };
        }
        return {
          key: row.key,
          priority: "持续",
          scope: `${row.customer} · ${row.platform} · ${row.customerProject}`,
          action: `当前阶段“${latest?.adoptionStage ?? "未明确／未进入"}”，持续跟踪最新证据`,
          owner: "产品企划",
        };
      }),
    [scopeRows, technology],
  );

  return (
    <div className="page-stack">
      <div className="view-filter-bar">
        <div>
          <span>选择技术</span>
          <Select
            value={technology || undefined}
            placeholder="暂无启用技术"
            onChange={setRequestedTechnology}
            options={technologies.map((value) => ({ value, label: value }))}
          />
        </div>
        <div className="filter-summary">
          <Tag color="success" icon={<CheckCircleOutlined />}>
            仅人工确认正式状态
          </Tag>
          <strong>{technology || "暂无技术"}</strong>
          <span>{new Set(scoped.map((item) => item.customer)).size} 个客户 · {scoped.length} 条职能观察</span>
        </div>
      </div>

      <div className="technology-grid">
        <Card className="content-card technology-grid__matrix">
          <SectionHeading
            eyebrow="FUNCTION MATRIX"
            title={`${technology} · 客户职能意向地图`}
          />
          {technology ? <div className="intent-matrix-wrap">
            <table className="intent-matrix">
              <thead>
                <tr>
                  <th>客户／平台／客户项目</th>
                  {FUNCTION_ORDER.map((item) => <th key={item}>{item}</th>)}
                  <th>采用阶段</th>
                  <th>关键卡点</th>
                </tr>
              </thead>
              <tbody>
                {scopeRows.map((row) => {
                  const anchor = latestFormalStatus(row.items);
                  return (
                    <tr key={row.key}>
                      <th>
                        <div className="table-stack">
                          <strong>{row.customer}</strong>
                          <span>{row.platform}</span>
                          <small>{row.customerProject}</small>
                        </div>
                      </th>
                      {FUNCTION_ORDER.map((functionName) => {
                        const intent = row.byFunction.get(functionName);
                        return (
                          <td key={functionName}>
                            <button
                              type="button"
                              className="matrix-cell"
                              disabled={!intent?.evidenceId}
                              onClick={() =>
                                intent &&
                                onEvidence(intent.evidenceId, intent.statusKey)
                              }
                              title={
                                intent?.evidenceId
                                  ? `查看${functionName}证据`
                                  : intent
                                    ? "当前正式状态暂无可下钻证据"
                                    : "尚无正式状态"
                              }
                            >
                              <AttitudeBadge
                                value={intent?.attitude ?? null}
                                compact
                              />
                              {intent ? (
                                <small>正式状态 V{intent.version}</small>
                              ) : (
                                <small>尚无正式状态</small>
                              )}
                            </button>
                          </td>
                        );
                      })}
                      <td>
                        {anchor ? (
                          <Tag color={adoptionStageColor[anchor.adoptionStage]}>
                            {anchor.adoptionStage}
                          </Tag>
                        ) : (
                          <span className="muted">未明确／未进入</span>
                        )}
                      </td>
                      <td className="matrix-blocker">
                        {anchor?.blocker ?? "尚未触达／证据不足"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div> : <Empty description="暂无启用技术" />}
        </Card>

        <Card className="content-card">
          <SectionHeading eyebrow="FUNCTION SIGNAL" title="职能态度构成" />
          <EChartPanel
            option={overviewOption}
            height={300}
            ariaLabel={`${technology}在各客户的职能态度数量构成`}
          />
          <p className="chart-note">
            每个色块代表一条已确认职能状态；不做客户平均分，也不形成客户总分。
          </p>
        </Card>
      </div>

      <Card className="content-card">
        <SectionHeading eyebrow="ACTION BOARD" title="基于当前正式状态的下一步提示" />
        <div className="action-board">
          {actions.map((item) => (
            <div className="action-row" key={item.key}>
              <Tag color={item.priority === "优先" ? "red" : item.priority === "跟进" ? "gold" : "default"}>
                {item.priority}
              </Tag>
              <strong>{item.scope}</strong>
              <span>{item.action}</span>
              <small>{item.owner}</small>
            </div>
          ))}
          {!actions.length ? <Empty description="暂无可生成提示的正式状态" /> : null}
        </div>
      </Card>
    </div>
  );
}

function CustomerPage({
  intents,
  dictionaries,
  onEvidence,
}: {
  intents: FormalIntentStatus[];
  dictionaries: DictionaryCollection;
  onEvidence: OpenEvidence;
}) {
  const customers = useMemo(() => {
    const active = dictionaries.customers
      .filter((item) => item.status === "启用")
      .map((item) => item.name);
    return active.length
      ? active
      : Array.from(new Set(intents.map((item) => item.customer)));
  }, [dictionaries.customers, intents]);
  const [requestedCustomer, setRequestedCustomer] = useState("客户A");
  const customer = customers.includes(requestedCustomer)
    ? requestedCustomer
    : customers[0] ?? "";
  const scoped = useMemo(
    () => intents.filter((item) => item.customer === customer),
    [customer, intents],
  );
  const technologies = Array.from(new Set(scoped.map((item) => item.technology)));
  const scopeRows = useMemo(
    () =>
      customer ? groupFormalIntentsByScope(intents, { customer }) : [],
    [customer, intents],
  );
  const evidenceCount = scoped.reduce(
    (total, item) => total + item.evidenceCount,
    0,
  );

  return (
    <div className="page-stack">
      <div className="customer-hero">
        <div className="customer-monogram">
          {customer ? customer.slice(-1) : "—"}
        </div>
        <div>
          <span>脱敏客户档案</span>
          <Title level={2}>{customer || "暂无启用客户"}</Title>
          <p>按技术与平台还原内部决策链，不给整家客户打一个总分。</p>
          <Tag color="success" icon={<CheckCircleOutlined />}>
            看板仅使用当前有效的人工确认正式状态
          </Tag>
        </div>
        <Select
          value={customer || undefined}
          placeholder="暂无启用客户"
          onChange={setRequestedCustomer}
          options={customers.map((value) => ({ value, label: value }))}
        />
      </div>

      <div className="customer-summary-strip">
        <div><span>当前覆盖技术</span><strong>{technologies.length}</strong></div>
        <div><span>已有职能证据</span><strong>{evidenceCount}</strong></div>
        <div><span>推动者</span><strong>{scoped.filter((item) => item.isSupporter).length}</strong></div>
        <div><span>阻力／把关</span><strong>{scoped.filter((item) => item.isResistance).length}</strong></div>
        <div><span>未知职能</span><strong>{scoped.filter((item) => item.attitude === null).length}</strong></div>
      </div>

      {scopeRows.length ? (
        <div className="customer-technology-list">
          {scopeRows.map((row) => {
            const items = [...row.byFunction.values()];
            const anchor = latestFormalStatus(items);
            const supporters = items.filter((item) => item.isSupporter);
            const blockers = items.filter((item) => item.isResistance);
            return (
              <Card className="content-card customer-tech-card" key={row.key}>
                <div className="customer-tech-card__head">
                  <div>
                    <span className="section-eyebrow">TECHNOLOGY</span>
                    <Title level={3}>{row.technology}</Title>
                    <p>{row.platform} · {row.customerProject}</p>
                  </div>
                  <div className="stage-block">
                    <span>客户技术采用阶段</span>
                    <Tag
                      color={
                        adoptionStageColor[
                          anchor?.adoptionStage ?? "未明确／未进入"
                        ]
                      }
                    >
                      {anchor?.adoptionStage ?? "未明确／未进入"}
                    </Tag>
                  </div>
                </div>
                <div className="decision-chain">
                  {FUNCTION_ORDER.map((functionName, index) => {
                    const item = row.byFunction.get(functionName);
                    return (
                      <React.Fragment key={functionName}>
                        <button
                          type="button"
                          className={`decision-node ${item ? "has-evidence" : ""}`}
                          onClick={() =>
                            item &&
                            onEvidence(item.evidenceId, item.statusKey)
                          }
                          disabled={!item?.evidenceId}
                          title={
                            item?.evidenceId
                              ? `查看${functionName}证据`
                              : item
                                ? "当前正式状态暂无可下钻证据"
                                : "尚无正式状态"
                          }
                        >
                          <span>{functionName}</span>
                          <AttitudeBadge value={item?.attitude ?? null} compact />
                          <small>
                            {item ? `正式状态 V${item.version}` : "尚无正式状态"}
                          </small>
                        </button>
                        {index < FUNCTION_ORDER.length - 1 ? (
                          <ArrowRightOutlined className="decision-arrow" />
                        ) : null}
                      </React.Fragment>
                    );
                  })}
                </div>
                <div className="customer-tech-card__bottom">
                  <div>
                    <span>当前支持者</span>
                    <strong>
                      {supporters.length
                        ? supporters.map((item) => item.functionName).join("、")
                        : "尚未识别"}
                    </strong>
                  </div>
                  <div>
                    <span>阻力／关键把关</span>
                    <strong>
                      {blockers.length
                        ? blockers.map((item) => `${item.functionName}：${item.blocker}`).join("；")
                        : "未发现明确阻力"}
                    </strong>
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      ) : (
        <Empty description="该客户暂无当前正式状态" />
      )}
    </div>
  );
}

function HistoryPage({
  history,
  intents,
  evidence,
  onEvidence,
}: {
  history: HistoryEvent[];
  intents: FormalIntentStatus[];
  evidence: Evidence[];
  onEvidence: OpenEvidence;
}) {
  const historyKeys = useMemo(
    () => Array.from(new Set(history.map((item) => item.intentId))),
    [history],
  );
  const [requestedIntentId, setRequestedIntentId] = useState(
    history[0]?.intentId ?? "",
  );
  const intentId = historyKeys.includes(requestedIntentId)
    ? requestedIntentId
    : historyKeys[0] ?? "";
  const scoped = history.filter((item) => item.intentId === intentId);
  const intent = intents.find((item) => item.statusKey === intentId);
  const scopedEvidenceIds = new Set(
    scoped.map((item) => item.evidenceId).filter(Boolean),
  );
  const scopedEvidence = evidence.filter((item) =>
    scopedEvidenceIds.has(item.id),
  );

  const columns: TableColumnsType<HistoryEvent> = [
    { title: "版本", dataIndex: "version", width: 120 },
    { title: "生效／记录时间", dataIndex: "date", width: 130 },
    {
      title: "态度",
      width: 136,
      render: (_, item) => <AttitudeBadge value={item.attitude} compact />,
    },
    {
      title: "采用阶段",
      width: 140,
      render: (_, item) => (
        <Tag color={adoptionStageColor[item.adoptionStage]}>
          {item.adoptionStage}
        </Tag>
      ),
    },
    {
      title: "变化说明",
      render: (_, item) => (
        <div className="table-stack">
          <strong>{item.title}</strong>
          <span>{item.description}</span>
        </div>
      ),
    },
    { title: "确认／生成方", dataIndex: "actor", width: 170 },
    {
      title: "证据",
      width: 86,
      render: (_, item) =>
        item.evidenceId ? (
          <Button
            type="link"
            onClick={() => onEvidence(item.evidenceId, item.intentId)}
          >
            下钻
          </Button>
        ) : (
          <span className="muted">无证据</span>
        ),
    },
  ];

  return (
    <div className="page-stack">
      <div className="view-filter-bar">
        <div>
          <span>选择意向对象</span>
          <Select
            showSearch
            optionFilterProp="label"
            value={intentId || undefined}
            placeholder="暂无正式状态历史"
            onChange={setRequestedIntentId}
            disabled={!historyKeys.length}
            className="history-select"
            options={historyKeys.map((value) => {
                const target = intents.find((item) => item.statusKey === value);
                return {
                  value,
                  label: target
                    ? `${target.customer} · ${target.technology} · ${target.platform} · ${target.customerProject} · ${target.functionName}`
                    : value,
                };
              })}
          />
        </div>
        {intent ? (
          <div className="current-state-summary">
            <span>当前最新状态</span>
            <AttitudeBadge value={intent.attitude} compact />
            <Tag color={adoptionStageColor[intent.adoptionStage]}>
              {intent.adoptionStage}
            </Tag>
          </div>
        ) : null}
      </div>

      <div className="history-layout">
        <Card className="content-card history-timeline-card">
          <SectionHeading eyebrow="CHANGE LOG" title="意向变化时间轴" />
          <Timeline
            mode="left"
            items={scoped.map((item, index) => {
              const content = (
                <>
                  <div><strong>{item.title}</strong><Tag>{item.version}</Tag></div>
                  <p>{item.description}</p>
                  <div>
                    <AttitudeBadge value={item.attitude} compact />
                    <Tag color={adoptionStageColor[item.adoptionStage]}>
                      {item.adoptionStage}
                    </Tag>
                  </div>
                </>
              );
              return {
              color: index === 0 ? "blue" : "gray",
              label: item.date,
              children: item.evidenceId ? (
                <button
                  type="button"
                  className="history-timeline-item"
                  onClick={() => onEvidence(item.evidenceId, item.intentId)}
                >
                  {content}
                </button>
              ) : (
                <div className="history-timeline-item">{content}</div>
              ),
            };
            })}
          />
        </Card>

        <Card className="content-card history-evidence-card">
          <SectionHeading eyebrow="EVIDENCE COUNT" title="当前对象证据" />
          <div className="evidence-count">
            <strong>{scopedEvidence.length}</strong>
            <span>条可追溯证据</span>
          </div>
          <div className="evidence-type-list">
            {["原始录音", "人工纪要", "会议PDF", "录屏画面"].map((type) => (
              <div key={type}>
                <span>{type}</span>
                <strong>{scopedEvidence.filter((item) => item.sourceType === type).length}</strong>
              </div>
            ))}
          </div>
          <div className="evidence-boundary">
            <SafetyCertificateOutlined />
            <p>
              新会议和模型重跑生成新观察，不覆盖旧记录；人工确认结果形成追加版本。
            </p>
          </div>
        </Card>
      </div>

      <Card className="content-card table-card">
        <SectionHeading eyebrow="VERSION TABLE" title="版本明细" />
        <Table
          rowKey="id"
          columns={columns}
          dataSource={scoped}
          pagination={false}
          scroll={{ x: 980 }}
        />
      </Card>
    </div>
  );
}

function DictionaryPage({
  dictionaries,
  setDictionaries,
}: {
  dictionaries: DictionaryCollection;
  setDictionaries: React.Dispatch<React.SetStateAction<DictionaryCollection>>;
}) {
  const [activeKey, setActiveKey] = useState<keyof DictionaryCollection>(
    "technologies",
  );
  const [addOpen, setAddOpen] = useState(false);
  const [form] = Form.useForm();

  const labels: Record<keyof DictionaryCollection, string> = {
    technologies: "技术",
    customers: "客户",
    platforms: "通用平台",
    projects: "客户项目",
    functions: "客户职能",
  };

  const columns: TableColumnsType<DictionaryItem> = [
    {
      title: labels[activeKey],
      dataIndex: "name",
      width: 220,
      render: (value, item) => (
        <div className="table-stack">
          <strong>{value}</strong>
          <span>{item.code}</span>
        </div>
      ),
    },
    {
      title: "别名／会议用语",
      dataIndex: "aliases",
      render: (values: string[]) => (
        <Space wrap size={[4, 4]}>
          {values.map((value) => <Tag key={value}>{value}</Tag>)}
        </Space>
      ),
    },
    { title: "说明／映射", dataIndex: "note", width: 260 },
    {
      title: "状态",
      dataIndex: "status",
      width: 90,
      render: (value) => <Tag color={value === "启用" ? "success" : "default"}>{value}</Tag>,
    },
    {
      title: "更新时间",
      dataIndex: "updatedAt",
      width: 150,
      render: (value: string) => (
        <span className="table-date" title={value}>
          {compactDateTime(value)}
        </span>
      ),
    },
  ];

  const handleAdd = (values: {
    name: string;
    code: string;
    aliases?: string;
    note?: string;
  }) => {
    setDictionaries((current) => ({
      ...current,
      [activeKey]: [
        {
          id: `DICT-${Date.now()}`,
          name: values.name,
          code: values.code,
          aliases: values.aliases
            ? values.aliases.split(/[,，]/).map((item) => item.trim())
            : [],
          note: values.note ?? "",
          status: "启用",
          updatedAt: "刚刚",
        },
        ...current[activeKey],
      ],
    }));
    setAddOpen(false);
    form.resetFields();
    message.success("演示字典项已加入当前页面");
  };

  return (
    <div className="page-stack">
      <div className="dictionary-summary">
        <div>
          <DatabaseOutlined />
          <span>主数据项</span>
          <strong>
            {Object.values(dictionaries).reduce(
              (sum, items) => sum + items.length,
              0,
            )}
          </strong>
        </div>
        <div>
          <SettingOutlined />
          <span>规则</span>
          <strong>AI只推荐，不自动新建</strong>
        </div>
        <div>
          <HistoryOutlined />
          <span>变更</span>
          <strong>保留版本与说明</strong>
        </div>
      </div>

      <Card className="content-card table-card">
        <div className="table-toolbar">
          <Tabs
            activeKey={activeKey}
            onChange={(key) =>
              setActiveKey(key as keyof DictionaryCollection)
            }
            items={Object.entries(labels).map(([key, label]) => ({
              key,
              label: `${label} ${dictionaries[key as keyof DictionaryCollection].length}`,
            }))}
          />
          <Space>
            <Upload
              beforeUpload={() => {
                message.info("演示模式：已完成XLSX字段预检，未写入服务端");
                return false;
              }}
              showUploadList={false}
              accept=".xlsx"
            >
              <Button icon={<CloudUploadOutlined />}>导入Excel</Button>
            </Upload>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => setAddOpen(true)}
            >
              新增{labels[activeKey]}
            </Button>
          </Space>
        </div>
        <Table
          rowKey="id"
          columns={columns}
          dataSource={dictionaries[activeKey]}
          pagination={false}
          scroll={{ x: 820 }}
        />
      </Card>

      <div className="dictionary-rules">
        <div><strong>标准名优先</strong><span>会议原文通过别名映射到统一主数据</span></div>
        <div><strong>平台分层</strong><span>通用产品平台与客户内部项目分别建模</span></div>
        <div><strong>人工治理</strong><span>模型无法覆盖正式字典，停用项保留历史引用</span></div>
      </div>

      <Modal
        open={addOpen}
        title={`新增${labels[activeKey]}`}
        okText="保存"
        cancelText="取消"
        onCancel={() => setAddOpen(false)}
        onOk={() => form.submit()}
      >
        <Form form={form} layout="vertical" onFinish={handleAdd}>
          <Form.Item name="name" label="标准名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="code" label="编码" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="aliases" label="别名（用逗号分隔）">
            <Input />
          </Form.Item>
          <Form.Item name="note" label="说明／映射">
            <Input.TextArea rows={3} />
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}

function IntentBaoShell() {
  const [activeView, setActiveView] = useState<ViewKey>("overview");
  const [collapsed, setCollapsed] = useState(false);
  const [dataset, setDataset] = useState<MvpDataset | null>(null);
  const [source, setSource] = useState<DataSourceMode>("loading");
  const [evidenceId, setEvidenceId] = useState<string | undefined>();
  const [evidenceStatusKey, setEvidenceStatusKey] = useState<
    string | undefined
  >();
  const [activePackageId, setActivePackageId] = useState<string>();

  const navigateTo = useCallback<NavigateTo>((key, packageId) => {
    setActiveView(key);
    setActivePackageId(packageId);
  }, []);

  const openEvidence = useCallback<OpenEvidence>((id, statusKey) => {
    if (!id) return;
    setEvidenceId(id);
    setEvidenceStatusKey(statusKey);
  }, []);

  const reloadDataset = useCallback(async () => {
    const data = await loadBootstrapDataset();
    setDataset(data);
    setSource("api");
    return data;
  }, []);

  useEffect(() => {
    let alive = true;
    void loadBootstrapDataset()
      .then((data) => {
        if (!alive) return;
        setDataset(data);
        setSource("api");
      })
      .catch(() => {
        if (!alive) return;
        setDataset(mockDataset);
        setSource("mock");
      });
    return () => {
      alive = false;
    };
  }, []);

  const updateDataset = <K extends keyof MvpDataset>(
    key: K,
    value: React.SetStateAction<MvpDataset[K]>,
  ) => {
    setDataset((current) => {
      if (!current) return current;
      const nextValue =
        typeof value === "function"
          ? (value as (previous: MvpDataset[K]) => MvpDataset[K])(current[key])
          : value;
      return { ...current, [key]: nextValue };
    });
  };

  const evidence = dataset?.evidence.find((item) => item.id === evidenceId);
  const evidenceIntent = dataset?.intents.find(
    (item) => item.evidenceId === evidenceId,
  );
  const formalEvidenceIntent = dataset?.formalIntents.find(
    (item) =>
      item.evidenceId === evidenceId ||
      (Boolean(evidenceStatusKey) && item.statusKey === evidenceStatusKey),
  );
  const evidenceHistory = (dataset?.history ?? []).filter(
    (item) =>
      item.intentId ===
      (evidenceStatusKey ??
        formalEvidenceIntent?.statusKey ??
        evidenceIntent?.id),
  );
  const currentMeta = pageMeta[activeView];
  const latestFormalUpdate = [...(dataset?.formalIntents ?? [])]
    .map((item) => item.confirmedAt)
    .filter(Boolean)
    .sort()
    .at(-1);

  const content = (() => {
    if (!dataset) {
      return (
        <Card className="content-card">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="正在连接本地API并加载意向数据…"
          />
          <Progress percent={35} showInfo={false} status="active" />
        </Card>
      );
    }
    const speakerMeetingIds = new Set(
      dataset.speakers.map((speaker) => speaker.meetingId),
    );
    const speakerMeetingId =
      (activePackageId && speakerMeetingIds.has(activePackageId)
        ? activePackageId
        : undefined) ??
      dataset.meetings.find(
        (meeting) =>
          meeting.status === "待身份校验" &&
          speakerMeetingIds.has(meeting.id),
      )?.id ??
      dataset.speakers[0]?.meetingId;
    switch (activeView) {
      case "meetings":
        return (
          <MeetingsPage
            meetings={dataset.meetings}
            onNavigate={navigateTo}
            source={source}
            reloadDataset={reloadDataset}
          />
        );
      case "speakers":
        return (
          <SpeakersPage
            speakers={dataset.speakers}
            meetings={dataset.meetings}
            meetingId={speakerMeetingId}
            setSpeakers={(value) => updateDataset("speakers", value)}
            onNavigate={navigateTo}
            onMeetingChange={setActivePackageId}
            reloadDataset={reloadDataset}
          />
        );
      case "insights":
        return (
          <InsightsPage
            intents={dataset.intents}
            evidence={dataset.evidence}
            meetingId={activePackageId}
            onEvidence={openEvidence}
            reloadDataset={reloadDataset}
          />
        );
      case "technology":
        return (
          <TechnologyPage
            intents={dataset.formalIntents}
            dictionaries={dataset.dictionaries}
            onEvidence={openEvidence}
          />
        );
      case "customer":
        return (
          <CustomerPage
            intents={dataset.formalIntents}
            dictionaries={dataset.dictionaries}
            onEvidence={openEvidence}
          />
        );
      case "history":
        return (
          <HistoryPage
            history={dataset.history}
            intents={dataset.formalIntents}
            evidence={dataset.evidence}
            onEvidence={openEvidence}
          />
        );
      case "dictionary":
        return (
          <DictionaryPage
            dictionaries={dataset.dictionaries}
            setDictionaries={(value) => updateDataset("dictionaries", value)}
          />
        );
      case "settings":
        return <ModelSettingsPage />;
      default:
        return (
          <OverviewPage
            data={dataset}
            onNavigate={navigateTo}
            onEvidence={openEvidence}
          />
        );
    }
  })();

  return (
    <Layout className="intent-app">
      <Sider
        width={236}
        collapsedWidth={76}
        collapsed={collapsed}
        className="app-sider"
        trigger={null}
      >
        <div className="brand-block">
          <div className="brand-mark" aria-hidden="true">
            <i />
            <i />
            <i />
          </div>
          {!collapsed ? (
            <div>
              <strong>意向宝</strong>
              <span>客户意向智能体</span>
            </div>
          ) : null}
        </div>
        <Menu
          mode="inline"
          selectedKeys={[activeView]}
          items={navItems}
          onClick={({ key }) => navigateTo(key as ViewKey)}
          className="app-menu"
        />
        <div className="sider-foot">
          {!collapsed ? (
            <>
              <span className="simulation-flag">
                {source === "loading" ? "数据加载中" : "模拟数据"}
              </span>
              <p>
                {source === "loading" ? "正在连接本地API" : "首期流程验证版"}
              </p>
            </>
          ) : (
            <Tooltip title={source === "loading" ? "数据加载中" : "模拟数据"}>
              <span className="simulation-dot">
                {source === "loading" ? "…" : "M"}
              </span>
            </Tooltip>
          )}
        </div>
      </Sider>

      <Layout>
        <header className="top-header">
          <Button
            type="text"
            aria-label={collapsed ? "展开导航" : "收起导航"}
            icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
            onClick={() => setCollapsed((value) => !value)}
            className="collapse-button"
          />
          <div className="top-header__title">
            <span>{currentMeta.eyebrow}</span>
            <strong>{currentMeta.title}</strong>
          </div>
          <div className="top-header__right">
            <Tooltip title={`API：${API_BASE}`}>
              <Tag
                icon={source === "mock" ? <WarningOutlined /> : <ApiOutlined />}
                color={source === "api" ? "success" : "gold"}
              >
                {source === "api"
                  ? "本地API"
                  : source === "loading"
                    ? "连接中"
                    : "演示回退"}
              </Tag>
            </Tooltip>
            <div className="operator-avatar">企</div>
            <div className="operator-copy">
              <strong>产品规划负责人</strong>
              <span>本地单用户模式</span>
            </div>
          </div>
        </header>

        <nav className="mobile-nav" aria-label="移动端主导航">
          {navItems
            ?.filter((item) => item && "key" in item && item.key)
            .map((item) => {
              if (!item || !("key" in item) || !item.key) return null;
              const menuItem = item as {
                key: React.Key;
                icon?: React.ReactNode;
                label?: React.ReactNode;
              };
              const key = String(menuItem.key) as ViewKey;
              return (
                <button
                  type="button"
                  key={key}
                  className={activeView === key ? "is-active" : ""}
                  onClick={() => navigateTo(key)}
                >
                  {menuItem.icon}
                  <span>{menuItem.label}</span>
                </button>
              );
            })}
        </nav>

        <DataScopeBanner source={source} />

        <Content className="app-content">
          {activeView !== "overview" ? (
            <div className="page-intro">
              <div>
                <span>{currentMeta.eyebrow}</span>
                <Title level={1}>{currentMeta.title}</Title>
                <Paragraph>{currentMeta.description}</Paragraph>
              </div>
              {activeView !== "settings" ? (
                <div className="page-intro__status">
                  <ClockCircleOutlined />
                  <span>最新正式状态</span>
                  <strong>{compactDateTime(latestFormalUpdate)}</strong>
                </div>
              ) : null}
            </div>
          ) : null}
          {content}
          <footer className="app-footer">
            <span>意向宝 · 初版流程验证</span>
            <span>
              {source === "loading"
                ? "正在读取本地数据；尚未展示内置演示业务记录"
                : "当前使用模拟数据；关键意向结构化准确率 85% 是后续授权样本验证目标，并非已实现成绩"}
            </span>
          </footer>
        </Content>
      </Layout>

      <EvidenceDrawer
        open={Boolean(evidenceId)}
        evidence={evidence}
        intent={evidenceIntent}
        formalIntent={formalEvidenceIntent}
        history={evidenceHistory}
        onClose={() => {
          setEvidenceId(undefined);
          setEvidenceStatusKey(undefined);
        }}
      />
    </Layout>
  );
}

export default function IntentBaoApp() {
  return (
    <ConfigProvider
      theme={{
        token: {
          colorPrimary: "#175ea6",
          colorInfo: "#175ea6",
          colorSuccess: "#128269",
          colorWarning: "#c47b16",
          colorError: "#b83c38",
          colorText: "#152239",
          colorTextSecondary: "#667085",
          colorBorderSecondary: "#e7ebf1",
          borderRadius: 8,
          fontFamily:
            '"Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", Arial, sans-serif',
        },
        components: {
          Button: { controlHeight: 36, fontWeight: 600 },
          Card: { paddingLG: 20 },
          Menu: {
            itemHeight: 44,
            itemBorderRadius: 6,
            itemSelectedBg: "#e8f1fb",
            itemSelectedColor: "#175ea6",
          },
          Table: {
            headerBg: "#f5f7fa",
            headerColor: "#475467",
            rowHoverBg: "#f7fbff",
          },
        },
      }}
    >
      <AntApp>
        <IntentBaoShell />
      </AntApp>
    </ConfigProvider>
  );
}
