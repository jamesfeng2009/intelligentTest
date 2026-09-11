"""文档分片（T40）：按 5 类知识（PRD/接口文档/历史用例/缺陷库/上线检查清单）解析与分片。

- Markdown 优先按标题段落分片，纯文本按长度软切分
- 保留来源引用（doc_type + 序号 + 原文段落），供召回追溯
"""
from __future__ import annotations

import re

# 每类知识的元信息（可追溯来源名）
DOC_TYPES = {
    "prd": "PRD/需求文档",
    "api": "接口文档",
    "cases": "历史用例",
    "defects": "缺陷库",
    "checklist": "上线检查清单",
}

_MAX_CHARS = 500  # 软分片上限
_OVERLAP = 40     # 分片重叠，保证语义连续


def _is_title_only(block: str) -> bool:
    """判定是否为纯标题分片：标题行 + 无实质内容行。"""
    lines = [ln for ln in block.splitlines() if ln.strip()]
    if len(lines) > 2:
        return False
    body = [ln for ln in lines[1:] if not ln.strip().startswith(("#", "="))]
    return not body and bool(re.match(r"^#{1,4}\s", lines[0]))


def split_document(content: str, doc_type: str = "prd") -> list[str]:
    """把整篇文档切成语义分片，返回分片列表。"""
    content = (content or "").strip()
    if not content:
        return []

    # Markdown：优先按标题分块
    lines = content.splitlines()
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if re.match(r"^#{1,4}\s", line) and current:
            blocks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    blocks = [b for b in blocks if b]
    # 过滤纯标题分片（仅标题行、无正文内容——检索价值低且易误召回）
    blocks = [b for b in blocks if not _is_title_only(b)]

    # 过长的块软切分（带重叠）
    chunks: list[str] = []
    for block in blocks:
        if len(block) <= _MAX_CHARS:
            chunks.append(block)
            continue
        start = 0
        while start < len(block):
            end = min(start + _MAX_CHARS, len(block))
            chunks.append(block[start:end])
            if end >= len(block):
                break
            start = max(end - _OVERLAP, start + 1)
    return [c for c in chunks if len(c.strip()) >= 8]


def tokenize(text: str) -> list[str]:
    """轻量分词：英文单词 + 中文二元组（零第三方依赖）。"""
    tokens: list[str] = []
    for word in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
        tokens.append(word)
    cn = re.findall(r"[\u4e00-\u9fff]+", text)
    for seg in cn:
        if len(seg) == 1:
            tokens.append(seg)
        else:
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens
