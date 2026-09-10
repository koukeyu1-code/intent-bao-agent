from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any


def estimate_tokens(text: str) -> int:
    """Conservative estimate; actual tokenization depends on the loaded model."""
    ascii_count = sum(char.isascii() for char in text)
    return math.ceil(ascii_count * 0.5 + (len(text) - ascii_count) * 1.5)


def split_items_for_request(
    items: list[dict[str, Any]],
    *,
    fits: Callable[[list[dict[str, Any]]], bool],
    text_key: str = "content",
) -> list[list[dict[str, Any]]]:
    """Keep all text and source metadata while fitting complete requests.

    ``fits`` must include fixed prompts, reference data, schema and output
    reserve. Offsets are relative to the original item's text, so a split
    preserves the evidence identity instead of inventing a new source.
    """
    if not fits([]):
        raise ValueError("提示词、主数据及输出预算已超出模型上下文，请减少主数据或核对服务上下文配置")
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for original in items:
        item = dict(original)
        if fits([*current, item]):
            current.append(item)
            continue
        if current:
            batches.append(current)
            current = []
        if fits([item]):
            current.append(item)
            continue
        content = item.get(text_key)
        if not isinstance(content, str) or not content:
            raise ValueError("单项材料元数据已超出模型上下文，无法在保留来源信息的情况下分批")
        cursor = 0
        part_index = 0
        base_offset = int(item.get("content_offset") or 0)
        while cursor < len(content):
            def fragment(size: int) -> dict[str, Any]:
                return {
                    **item,
                    text_key: content[cursor:cursor + size],
                    "part_index": part_index,
                    "content_offset": base_offset + cursor,
                    "content_end": base_offset + cursor + size,
                }

            low, high = 0, len(content) - cursor
            while low < high:
                middle = (low + high + 1) // 2
                if fits([fragment(middle)]):
                    low = middle
                else:
                    high = middle - 1
            if low == 0:
                raise ValueError("单项材料元数据已超出模型上下文，无法继续分批")
            current = [fragment(low)]
            cursor += low
            part_index += 1
            if cursor < len(content):
                batches.append(current)
                current = []
    if current:
        batches.append(current)
    return batches or [[]]
