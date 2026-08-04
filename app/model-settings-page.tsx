"use client";

import {
  ApiOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  CloudServerOutlined,
  DeleteOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
  SaveOutlined,
} from "@ant-design/icons";
import {
  AutoComplete,
  Button,
  Card,
  Descriptions,
  Divider,
  Form,
  Input,
  Popconfirm,
  Select,
  Space,
  Spin,
  Tag,
  Typography,
  message,
} from "antd";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  loadEnvironmentCheck,
  listAvailableModels,
  loadModelSettings,
  saveModelSettings,
  testModelConnection,
  type EnvironmentCheck,
} from "./api-client";
import type {
  ModelConnectionResult,
  ModelProvider,
  ModelSettings,
  ModelSettingsInput,
} from "./types";

const { Paragraph, Title } = Typography;

const OLLAMA_DEFAULT_URL = "http://127.0.0.1:11434";
const MOCK_MODEL_NAME = "mock-intent-v1";

type ModelOperation = "models" | "test" | "save" | "clear-key";

const defaultSettings: ModelSettings = {
  provider: "mock",
  baseUrl: OLLAMA_DEFAULT_URL,
  modelName: MOCK_MODEL_NAME,
  asrProvider: "mock",
  asrBaseUrl: "http://127.0.0.1:8081",
  asrModelName: "",
  apiKeyConfigured: false,
  connectionStatus: "unknown",
};

const LLAMACPP_DEFAULT_URL = "http://127.0.0.1:8099";

const providerMeta: Record<
  ModelProvider,
  { label: string; description: string; color: string }
> = {
  mock: {
    label: "模拟分析",
    description: "继续使用固定模拟结果，只验证业务流程，不代表真实模型能力。",
    color: "gold",
  },
  ollama: {
    label: "本地 Ollama",
    description: "由意向宝后端调用本机 Ollama，浏览器不会直接持有模型密钥。",
    color: "green",
  },
};

const statusMeta: Record<
  ModelSettings["connectionStatus"],
  { label: string; color: string }
> = {
  unknown: { label: "尚未检测", color: "default" },
  checking: { label: "检测中", color: "processing" },
  connected: { label: "已连接", color: "success" },
  error: { label: "连接失败", color: "error" },
};

const formatCheckedAt = (value?: string) => {
  if (!value) return "尚未检测";
  return value.replace("T", " ").slice(0, 19);
};

export default function ModelSettingsPage() {
  const [form] = Form.useForm<ModelSettingsInput>();
  const provider = Form.useWatch("provider", form) ?? "mock";
  const baseUrl = Form.useWatch("baseUrl", form);
  const modelName = Form.useWatch("modelName", form);
  const [settings, setSettings] =
    useState<ModelSettings>(defaultSettings);
  const [loading, setLoading] = useState(true);
  const activeOperationRef = useRef<ModelOperation | undefined>(undefined);
  const [activeOperation, setActiveOperation] =
    useState<ModelOperation>();
  const [modelOptions, setModelOptions] = useState<string[]>([
    MOCK_MODEL_NAME,
  ]);
  const [testResult, setTestResult] =
    useState<ModelConnectionResult>();
  const [envCheck, setEnvCheck] = useState<EnvironmentCheck>();
  const [asrChecking, setAsrChecking] = useState(false);
  const operationBusy = activeOperation !== undefined;

  const beginOperation = (operation: ModelOperation) => {
    if (activeOperationRef.current) return false;
    activeOperationRef.current = operation;
    setActiveOperation(operation);
    return true;
  };

  const finishOperation = (operation: ModelOperation) => {
    if (activeOperationRef.current !== operation) return;
    activeOperationRef.current = undefined;
    setActiveOperation(undefined);
  };

  const applySettingsToForm = useCallback(
    (next: ModelSettings) => {
      form.setFieldsValue({
        provider: next.provider,
        baseUrl: next.baseUrl || OLLAMA_DEFAULT_URL,
        modelName:
          next.modelName ||
          (next.provider === "mock" ? MOCK_MODEL_NAME : ""),
        asrProvider: next.asrProvider || "mock",
        asrBaseUrl: next.asrBaseUrl || LLAMACPP_DEFAULT_URL,
        asrModelName: next.asrModelName || "",
        apiKey: "",
      });
    },
    [form],
  );

  useEffect(() => {
    let alive = true;
    void loadEnvironmentCheck()
      .then((check) => {
        if (alive) setEnvCheck(check);
      })
      .catch(() => undefined);
    void loadModelSettings()
      .then((next) => {
        if (!alive) return;
        setSettings(next);
        applySettingsToForm(next);
        setModelOptions((current) =>
          Array.from(
            new Set(
              [
                ...current,
                next.modelName,
                next.provider === "mock" ? MOCK_MODEL_NAME : "",
              ].filter(Boolean),
            ),
          ),
        );
      })
      .catch((error) => {
        if (!alive) return;
        applySettingsToForm(defaultSettings);
        const reason =
          error instanceof Error ? error.message : "本地API暂不可用";
        message.error(`模型设置读取失败：${reason}`);
      })
      .finally(() => {
        if (alive) setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [applySettingsToForm]);

  const currentInput = useCallback(async (): Promise<ModelSettingsInput> => {
    const values = await form.validateFields();
    return {
      provider: values.provider,
      baseUrl:
        values.provider === "mock"
          ? values.baseUrl || OLLAMA_DEFAULT_URL
          : values.baseUrl,
      modelName:
        values.provider === "mock"
          ? values.modelName || MOCK_MODEL_NAME
          : values.modelName,
      asrProvider: values.asrProvider ?? "mock",
      asrBaseUrl: values.asrBaseUrl || "",
      asrModelName: values.asrModelName || "",
      ...(values.apiKey ? { apiKey: values.apiKey } : {}),
    };
  }, [form]);

  const handleCheckAsr = async () => {
    if (asrChecking) return;
    setAsrChecking(true);
    try {
      const check = await loadEnvironmentCheck();
      setEnvCheck(check);
      if (check.asr.enabled && check.asr_healthy) {
        message.success("ASR 服务连接正常");
      } else if (check.asr.enabled) {
        message.warning(
          `ASR 服务（${check.asr.base_url}）暂不可达，请确认已启动`,
        );
      } else {
        message.info("ASR 当前为模拟模式，不调用外部服务");
      }
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : "本地API暂不可用";
      message.error(`环境自检失败：${reason}`);
    } finally {
      setAsrChecking(false);
    }
  };

  const handleProviderChange = (value: ModelProvider) => {
    setTestResult(undefined);
    setSettings((current) => ({
      ...current,
      connectionStatus: "unknown",
      lastError: undefined,
    }));
    if (value === "mock") {
      form.setFieldsValue({
        modelName: MOCK_MODEL_NAME,
        apiKey: "",
      });
      setModelOptions([MOCK_MODEL_NAME]);
      return;
    }
    form.setFieldsValue({
      baseUrl: form.getFieldValue("baseUrl") || OLLAMA_DEFAULT_URL,
      modelName:
        form.getFieldValue("modelName") === MOCK_MODEL_NAME
          ? ""
          : form.getFieldValue("modelName"),
      apiKey: "",
    });
    setModelOptions((current) =>
      current.filter((item) => item !== MOCK_MODEL_NAME),
    );
  };

  const handleLoadModels = async () => {
    if (!beginOperation("models")) return;
    try {
      await form.validateFields(["provider", "baseUrl"]);
      const values = form.getFieldsValue();
      const input: ModelSettingsInput = {
        provider: values.provider,
        baseUrl: values.baseUrl || OLLAMA_DEFAULT_URL,
        modelName: values.modelName || "",
        ...(values.apiKey ? { apiKey: values.apiKey } : {}),
      };
      const models = await listAvailableModels(input);
      setModelOptions(models);
      if (!models.length) {
        message.warning("服务已响应，但没有发现可用模型；可以手工输入模型名称");
      } else {
        message.success(`已读取 ${models.length} 个可用模型`);
      }
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : "模型服务暂不可用";
      message.error(`读取模型列表失败：${reason}`);
    } finally {
      finishOperation("models");
    }
  };

  const handleTest = async () => {
    if (!beginOperation("test")) return;
    setTestResult(undefined);
    setSettings((current) => ({
      ...current,
      connectionStatus: "checking",
    }));
    try {
      const input = await currentInput();
      const result = await testModelConnection(input);
      setTestResult(result);
      setModelOptions((current) =>
        Array.from(new Set([...current, ...result.models].filter(Boolean))),
      );
      setSettings((current) => ({
        ...current,
        provider: result.provider,
        baseUrl: result.baseUrl,
        modelName: result.modelName,
        connectionStatus: result.ok ? "connected" : "error",
        lastCheckedAt: result.checkedAt,
        lastError: result.ok ? undefined : result.message,
      }));
      if (result.ok) {
        message.success(result.message || "模型连接测试成功");
      } else {
        message.error(result.message || "模型连接测试失败");
      }
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : "模型服务暂不可用";
      setSettings((current) => ({
        ...current,
        connectionStatus: "error",
        lastError: reason,
      }));
      message.error(`模型连接测试失败：${reason}`);
    } finally {
      finishOperation("test");
    }
  };

  const handleSave = async () => {
    if (!beginOperation("save")) return;
    try {
      const input = await currentInput();
      const next = await saveModelSettings(input);
      setSettings(next);
      applySettingsToForm(next);
      setTestResult(undefined);
      message.success("模型设置已保存并应用");
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : "本地API暂不可用";
      message.error(`模型设置保存失败：${reason}`);
    } finally {
      finishOperation("save");
    }
  };

  const handleClearApiKey = async () => {
    if (!beginOperation("clear-key")) return;
    try {
      const input = await currentInput();
      const next = await saveModelSettings({
        ...input,
        apiKey: undefined,
        clearApiKey: true,
      });
      setSettings(next);
      applySettingsToForm(next);
      setTestResult(undefined);
      message.success("已清除服务端保存的 API Key");
    } catch (error) {
      const reason =
        error instanceof Error ? error.message : "本地API暂不可用";
      message.error(`清除 API Key 失败：${reason}`);
    } finally {
      finishOperation("clear-key");
    }
  };

  const connectionMeta = statusMeta[settings.connectionStatus];
  const modelOptionItems = useMemo(
    () => modelOptions.map((value) => ({ value })),
    [modelOptions],
  );

  return (
    <Spin spinning={loading}>
      <div className="page-stack">
        <div className="confirmation-principle model-settings-principle">
          <SafetyCertificateOutlined />
          <div>
            <strong>模型配置与业务数据分离</strong>
            <span>
              API Key 只提交到本机后端，不写入 localStorage，也不会由设置查询接口返回明文。
            </span>
          </div>
          <Tag color={connectionMeta.color}>{connectionMeta.label}</Tag>
        </div>

        <div className="model-settings-grid">
          <Card className="content-card model-settings-form-card">
            <Title level={3}>推理服务</Title>
            <Paragraph>
              选择模拟适配器，或连接本机已经运行的 Ollama 服务。模型供应商差异由后端封装。
            </Paragraph>

            <Form
              form={form}
              layout="vertical"
              disabled={loading || operationBusy}
              requiredMark="optional"
              initialValues={{
                provider: "mock",
                baseUrl: OLLAMA_DEFAULT_URL,
                modelName: MOCK_MODEL_NAME,
                asrProvider: "mock",
                asrBaseUrl: LLAMACPP_DEFAULT_URL,
                asrModelName: "",
              }}
              onValuesChange={() => {
                setTestResult(undefined);
                setSettings((current) => ({
                  ...current,
                  connectionStatus: "unknown",
                  lastError: undefined,
                }));
              }}
            >
              <Form.Item
                name="provider"
                label="服务类型"
                rules={[{ required: true, message: "请选择服务类型" }]}
              >
                <Select
                  onChange={handleProviderChange}
                  options={[
                    { value: "mock", label: "模拟分析" },
                    { value: "ollama", label: "本地 Ollama" },
                  ]}
                />
              </Form.Item>

              <Form.Item
                name="baseUrl"
                label="服务地址"
                tooltip="本地 Ollama 默认地址为 http://127.0.0.1:11434"
                rules={[
                  {
                    validator: (_, value) => {
                      if (provider === "mock") return Promise.resolve();
                      if (!value) {
                        return Promise.reject(new Error("请输入 Ollama 服务地址"));
                      }
                      if (
                        !/^http:\/\/(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?\/?$/i.test(
                          value.trim(),
                        )
                      ) {
                        return Promise.reject(
                          new Error(
                            "仅允许本机回环地址，例如 http://127.0.0.1:11434",
                          ),
                        );
                      }
                      return Promise.resolve();
                    },
                  },
                ]}
              >
                <Input
                  prefix={<ApiOutlined />}
                  placeholder={OLLAMA_DEFAULT_URL}
                  disabled={provider === "mock"}
                />
              </Form.Item>

              <Form.Item
                name="modelName"
                label="模型名称"
                rules={[{ required: true, message: "请选择或输入模型名称" }]}
                extra={
                  provider === "ollama"
                    ? "可从服务读取模型，也可以直接输入 Ollama 中的完整模型标签。"
                    : "模拟适配器固定使用 mock-intent-v1。"
                }
              >
                <AutoComplete
                  options={modelOptionItems}
                  placeholder={
                    provider === "mock"
                      ? MOCK_MODEL_NAME
                      : "例如 qwen3:8b"
                  }
                  disabled={provider === "mock"}
                  filterOption={(inputValue, option) =>
                    String(option?.value ?? "")
                      .toLowerCase()
                      .includes(inputValue.toLowerCase())
                  }
                />
              </Form.Item>

              <div className="model-list-action">
                <Button
                  icon={<ReloadOutlined />}
                  loading={activeOperation === "models"}
                  disabled={operationBusy || provider !== "ollama"}
                  onClick={() => void handleLoadModels()}
                >
                  读取本地模型
                </Button>
                <span>
                  {provider === "ollama"
                    ? `已发现 ${modelOptions.length} 个候选模型`
                    : "模拟模式无需读取模型列表"}
                </span>
              </div>

              <Form.Item
                name="apiKey"
                label="API Key（可选）"
                extra={
                  settings.apiKeyConfigured
                    ? "服务端已有密钥；留空保存会继续保留，页面不会读取原密钥。"
                    : "原生本地 Ollama 通常不需要密钥；带认证的本地代理可以在此配置。"
                }
              >
                <Input.Password
                  autoComplete="new-password"
                  placeholder={
                    settings.apiKeyConfigured
                      ? "已配置 · 留空则保持不变"
                      : "未配置"
                  }
                  disabled={provider === "mock"}
                />
              </Form.Item>

              <Divider plain>语音识别（ASR）服务</Divider>
              <Paragraph type="secondary">
                意向宝通过本地 llama.cpp 运行 Qwen3-ASR 完成录音转写；关闭时使用模拟转写。
              </Paragraph>

              <Form.Item
                name="asrProvider"
                label="ASR 服务类型"
                rules={[{ required: true, message: "请选择 ASR 服务类型" }]}
              >
                <Select
                  options={[
                    { value: "mock", label: "模拟转写" },
                    { value: "llamacpp", label: "本地 llama.cpp" },
                  ]}
                />
              </Form.Item>

              <Form.Item
                name="asrBaseUrl"
                label="ASR 服务地址"
                tooltip="llama-server 默认地址为 http://127.0.0.1:8099"
                rules={[
                  {
                    validator: (_, value) => {
                      if (form.getFieldValue("asrProvider") !== "llamacpp") {
                        return Promise.resolve();
                      }
                      if (!value) {
                        return Promise.reject(
                          new Error("请输入 ASR 服务地址"),
                        );
                      }
                      if (
                        !/^http:\/\/(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?\/?$/i.test(
                          value.trim(),
                        )
                      ) {
                        return Promise.reject(
                          new Error("仅允许本机回环地址，例如 http://127.0.0.1:8099"),
                        );
                      }
                      return Promise.resolve();
                    },
                  },
                ]}
              >
                <Input
                  prefix={<CloudServerOutlined />}
                  placeholder={LLAMACPP_DEFAULT_URL}
                  disabled={form.getFieldValue("asrProvider") !== "llamacpp"}
                />
              </Form.Item>

              <Form.Item
                name="asrModelName"
                label="ASR 模型名称"
                extra="llama-server 启动时加载的模型标识，例如 qwen3-asr"
                rules={[
                  {
                    validator: (_, value) => {
                      if (form.getFieldValue("asrProvider") !== "llamacpp") {
                        return Promise.resolve();
                      }
                      if (!value) {
                        return Promise.reject(
                          new Error("请输入 ASR 模型名称"),
                        );
                      }
                      return Promise.resolve();
                    },
                  },
                ]}
              >
                <Input
                  placeholder="qwen3-asr"
                  disabled={form.getFieldValue("asrProvider") !== "llamacpp"}
                />
              </Form.Item>

              <div className="model-settings-actions">
                <Space wrap>
                  <Button
                    icon={<ApiOutlined />}
                    loading={activeOperation === "test"}
                    disabled={operationBusy}
                    onClick={() => void handleTest()}
                  >
                    测试连接
                  </Button>
                  <Button
                    type="primary"
                    icon={<SaveOutlined />}
                    loading={activeOperation === "save"}
                    disabled={operationBusy}
                    onClick={() => void handleSave()}
                  >
                    保存并应用
                  </Button>
                </Space>
                <Popconfirm
                  title="清除 API Key"
                  description="只清除服务端保存的模型密钥，其他模型设置保持不变。"
                  okText="确认清除"
                  cancelText="取消"
                  disabled={
                    operationBusy ||
                    !settings.apiKeyConfigured ||
                    provider === "mock"
                  }
                  onConfirm={() => void handleClearApiKey()}
                >
                  <Button
                    danger
                    icon={<DeleteOutlined />}
                    loading={activeOperation === "clear-key"}
                    disabled={
                      operationBusy ||
                      !settings.apiKeyConfigured ||
                      provider === "mock"
                    }
                  >
                    清除密钥
                  </Button>
                </Popconfirm>
              </div>
            </Form>
          </Card>

          <Card className="content-card model-status-card">
            <div className="model-status-card__head">
              <div
                className={`model-status-icon model-status-icon--${settings.connectionStatus}`}
              >
                {settings.connectionStatus === "connected" ? (
                  <CheckCircleOutlined />
                ) : settings.connectionStatus === "error" ? (
                  <CloseCircleOutlined />
                ) : (
                  <ApiOutlined />
                )}
              </div>
              <div>
                <span>当前活动配置</span>
                <strong>{providerMeta[provider].label}</strong>
              </div>
              <Tag color={providerMeta[provider].color}>
                {provider === "ollama" ? "本地推理" : "流程演示"}
              </Tag>
            </div>

            <Descriptions column={1} size="small" bordered>
              <Descriptions.Item label="连接状态">
                <Tag color={connectionMeta.color}>{connectionMeta.label}</Tag>
              </Descriptions.Item>
              <Descriptions.Item label="当前模型">
                {modelName || "尚未选择"}
              </Descriptions.Item>
              <Descriptions.Item label="服务地址">
                {provider === "mock"
                  ? "不调用外部模型服务"
                  : baseUrl || OLLAMA_DEFAULT_URL}
              </Descriptions.Item>
              <Descriptions.Item label="API Key">
                {settings.apiKeyConfigured ? "已配置（内容已隐藏）" : "未配置"}
              </Descriptions.Item>
              <Descriptions.Item label="ASR 服务">
                {settings.asrProvider === "llamacpp"
                  ? `${settings.asrModelName || "未命名模型"} · ${
                      settings.asrBaseUrl || LLAMACPP_DEFAULT_URL
                    }`
                  : "模拟转写（不调用外部服务）"}
              </Descriptions.Item>
              <Descriptions.Item label="ASR 健康状态">
                {envCheck ? (
                  envCheck.asr.enabled ? (
                    <Tag color={envCheck.asr_healthy ? "success" : "error"}>
                      {envCheck.asr_healthy ? "已连接" : "不可达"}
                    </Tag>
                  ) : (
                    <Tag>模拟模式</Tag>
                  )
                ) : (
                  <Tag>未检测</Tag>
                )}
                <Button
                  type="link"
                  size="small"
                  icon={<ReloadOutlined />}
                  loading={asrChecking}
                  onClick={() => void handleCheckAsr()}
                >
                  重新检测
                </Button>
              </Descriptions.Item>
              <Descriptions.Item label="最近检测">
                {formatCheckedAt(
                  testResult?.checkedAt ?? settings.lastCheckedAt,
                )}
              </Descriptions.Item>
            </Descriptions>

            {testResult ? (
              <div
                className={`model-test-result ${
                  testResult.ok ? "is-success" : "is-error"
                }`}
                role="status"
              >
                {testResult.ok ? (
                  <CheckCircleOutlined />
                ) : (
                  <CloseCircleOutlined />
                )}
                <div>
                  <strong>{testResult.message}</strong>
                  <span>
                    {testResult.latencyMs !== undefined
                      ? `响应耗时 ${testResult.latencyMs} ms`
                      : "服务已返回检测结果"}
                  </span>
                </div>
              </div>
            ) : settings.lastError ? (
              <div className="model-test-result is-error" role="status">
                <CloseCircleOutlined />
                <div>
                  <strong>最近一次连接失败</strong>
                  <span>{settings.lastError}</span>
                </div>
              </div>
            ) : null}

            <div className="model-settings-note">
              <strong>能力边界</strong>
              <p>{providerMeta[provider].description}</p>
              <p>
                连接成功只表示服务可调用，不代表意向识别准确率已经达到85%；真实结果仍需人工确认。
              </p>
            </div>
          </Card>
        </div>
      </div>
    </Spin>
  );
}
