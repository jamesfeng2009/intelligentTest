"""文档分片（T40 / P1 父子拆分）：按 5 类知识（PRD/接口文档/历史用例/缺陷库/上线检查清单）解析与分片。

- 父块（Parent）：Markdown 标题章节 / 需求条目级，800~1500 字符，标题为锚点；命中后全文进 LLM 上下文
- 子块（Child）：150~400 字符，40 重叠；只建向量索引，命中子块取父块全文（解决"小块召回准、小块上下文碎"）
- 保留来源引用（doc_type + 序号 + 原文段落），供召回追溯
- split_document（扁平）保留兼容旧调用
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

_MAX_CHARS = 500  # 软分片上限（旧接口兼容）
_OVERLAP = 40     # 分片重叠，保证语义连续

# 父子拆分参数（P1）
PARENT_MAX = 1500    # 父块软上限（超长章节软切为多个父块）
CHILD_MAX = 400      # 子块软上限
CHILD_MIN = 150      # 子块软下限（低于下限不继续切）
CHILD_OVERLAP = 40   # 子块重叠


def _is_title_only(block: str) -> bool:
    """判定是否为纯标题分片：标题行 + 无实质内容行。"""
    lines = [ln for ln in block.splitlines() if ln.strip()]
    if len(lines) > 2:
        return False
    body = [ln for ln in lines[1:] if not ln.startswith(("#", "="))]
    return not body and bool(re.match(r"^#{1,4}\s", lines[0]))


def _title_blocks(content: str) -> list[dict]:
    """按 Markdown 标题切块，保留标题与层级（父块锚点）。"""
    lines = (content or "").splitlines()
    blocks: list[dict] = []
    current_title, current_level, current_lines = "", 0, []
    for line in lines:
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m and current_lines:
            blocks.append({"title": current_title, "level": current_level,
                           "text": "\n".join(current_lines).strip()})
            current_title, current_level, current_lines = m.group(2).strip(), len(m.group(1)), [line]
        elif m:
            current_title, current_level, current_lines = m.group(2).strip(), len(m.group(1)), [line]
        else:
            current_lines.append(line)
    if current_lines:
        blocks.append({"title": current_title, "level": current_level,
                       "text": "\n".join(current_lines).strip()})
    return [b for b in blocks if b["text"].strip()]


def _soft_split(text: str, limit: int, overlap: int = 0) -> list[str]:
    """软切分（带可选重叠）。"""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        out.append(text[start:end])
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return out


def split_document_structured(content: str, doc_type: str = "prd") -> list[dict]:
    """父子拆分：返回 [{title, level, parent_content, children: [{seq, content}]}]。

    - 父块：标题章节（超长按 PARENT_MAX 软切）
    - 子块：父块内容按 CHILD_MAX/CHILD_MIN/CHILD_OVERLAP 切分（唯一建向量索引的单元）
    - 纯标题父块（无正文）被过滤
    """
    parents: list[dict] = []
    for b in _title_blocks(content):
        if _is_title_only(b["text"]):
            continue
        for i, seg in enumerate(_soft_split(b["text"], PARENT_MAX)):
            children: list[dict] = []
            if len(seg) <= CHILD_MAX:
                children.append({"seq": 0, "content": seg})
            else:
                for j, c in enumerate(_soft_split(seg, CHILD_MAX, CHILD_OVERLAP)):
                    if len(c.strip()) >= CHILD_MIN or j == 0:
                        children.append({"seq": j, "content": c})
            parents.append({
                "title": b["title"], "level": b["level"],
                "parent_content": seg, "children": children,
            })
    return parents


def split_document(content: str, doc_type: str = "prd") -> list[str]:
    """把整篇文档切成语义分片，返回分片列表（旧接口：等价于子块内容）。"""
    out: list[str] = []
    for p in split_document_structured(content, doc_type):
        for c in p["children"]:
            out.append(c["content"])
    return [c for c in out if len(c.strip()) >= 8]


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
