"use client";

import { Button, Select, Space } from "antd";
import { useId, useMemo } from "react";

import {
  getPlatformCascadeModel,
  type PlatformCascadeSelection,
  type PlatformTaxonomy,
  type PlatformTaxonomyLevel,
  type TechnologyPlatformTaxonomy,
} from "./platform-taxonomy";

export interface PlatformContentRequest {
  technology: TechnologyPlatformTaxonomy;
  menu: PlatformTaxonomyLevel;
  parentLevel2ValueId?: string;
}

export interface PlatformCascadeSelectProps {
  taxonomy: PlatformTaxonomy;
  value: PlatformCascadeSelection;
  onChange: (selection: PlatformCascadeSelection) => void;
  onRequestNewContent?: (request: PlatformContentRequest) => void;
  technologyLabel?: string;
  disabled?: boolean;
  className?: string;
}

interface CascadeFieldProps {
  id: string;
  label: string;
  value?: string;
  placeholder: string;
  options: Array<{ value: string; label: string }>;
  disabled?: boolean;
  onChange: (value: string) => void;
  onClear?: () => void;
  onRequestNewContent?: () => void;
}

function CascadeField({
  id,
  label,
  value,
  placeholder,
  options,
  disabled,
  onChange,
  onClear,
  onRequestNewContent,
}: CascadeFieldProps) {
  return (
    <div className="platform-cascade-select__field">
      <label htmlFor={id}>{label}</label>
      <Select
        id={id}
        aria-label={label}
        value={value}
        placeholder={placeholder}
        options={options}
        disabled={disabled}
        allowClear={Boolean(onClear)}
        onChange={onChange}
        onClear={onClear}
      />
      {onRequestNewContent ? (
        <Button
          type="link"
          size="small"
          onClick={onRequestNewContent}
          disabled={disabled}
          aria-label={`新增${label}内容`}
        >
          新增内容
        </Button>
      ) : null}
    </div>
  );
}

/**
 * Controlled, reusable technology → level 2 → level 3 platform filter.
 * Use this exact component and the matching helper in both perspective pages
 * so their cascade semantics cannot drift apart.
 */
export function PlatformCascadeSelect({
  taxonomy,
  value,
  onChange,
  onRequestNewContent,
  technologyLabel = "选择技术",
  disabled = false,
  className,
}: PlatformCascadeSelectProps) {
  const reactId = useId();
  const model = useMemo(
    () => getPlatformCascadeModel(taxonomy, value),
    [taxonomy, value],
  );
  const selection = model.selection;

  return (
    <Space
      wrap
      align="end"
      className={["platform-cascade-select", className]
        .filter(Boolean)
        .join(" ")}
      role="group"
      aria-label="产品平台筛选"
    >
      <CascadeField
        id={`${reactId}-technology`}
        label={technologyLabel}
        value={selection.technologyId}
        placeholder="请选择技术"
        options={model.technologyOptions}
        disabled={disabled || model.technologyOptions.length === 0}
        onChange={(technologyId) => onChange({ technologyId })}
      />

      {model.level2 ? (
        <CascadeField
          id={`${reactId}-level-2`}
          label={model.level2.name}
          value={selection.level2ValueId}
          placeholder={`请选择${model.level2.name}`}
          options={model.level2Options}
          disabled={disabled || !selection.technologyId}
          onChange={(level2ValueId) =>
            onChange({
              technologyId: selection.technologyId,
              level2ValueId,
            })
          }
          onClear={() =>
            onChange({ technologyId: selection.technologyId })
          }
          onRequestNewContent={
            onRequestNewContent && model.technology
              ? () =>
                  onRequestNewContent({
                    technology: model.technology!,
                    menu: model.level2!,
                  })
              : undefined
          }
        />
      ) : null}

      {model.level3 ? (
        <CascadeField
          id={`${reactId}-level-3`}
          label={model.level3.name}
          value={selection.level3ValueId}
          placeholder={
            selection.level2ValueId
              ? `请选择${model.level3.name}`
              : `请先选择${model.level2?.name ?? "二级平台"}`
          }
          options={model.level3Options}
          disabled={disabled || !selection.level2ValueId}
          onChange={(level3ValueId) =>
            onChange({
              technologyId: selection.technologyId,
              level2ValueId: selection.level2ValueId,
              level3ValueId,
            })
          }
          onClear={() =>
            onChange({
              technologyId: selection.technologyId,
              level2ValueId: selection.level2ValueId,
            })
          }
          onRequestNewContent={
            onRequestNewContent && model.technology && selection.level2ValueId
              ? () =>
                  onRequestNewContent({
                    technology: model.technology!,
                    menu: model.level3!,
                    parentLevel2ValueId: selection.level2ValueId,
                  })
              : undefined
          }
        />
      ) : null}
    </Space>
  );
}
