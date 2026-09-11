"""结果回流（T51）：测试结果写回 PR 评论 / 消息通知。

- 生成 PR 评论 Markdown：关键结论 + 报告链接 + 失败明细
- 提供 message 通知模板（可对接 IM/邮件）
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from web.models import Report, Task


def build_pr_comment(db: Session, task_id: int) -> str:
    """根据任务结果生成 PR 评论（关键结论 + 报告链接 + 失败明细）。"""
    task = db.get(Task, task_id)
    if task is None:
        return "（任务不存在）"
    result = task.result or {}
    passed = result.get("passed", 0)
    failed = result.get("failed", 0)
    total = result.get("total", 0)
    rate = result.get("passed_rate", 0.0)

    lines = [
        f"## 🤖 AI 测试智能体 · 回归结果（{task.task_type}）",
        "",
        f"- **任务**：{task.title}",
        f"- **通过率**：{passed}/{total}（{rate}%）",
        f"- **状态**：{'✅ 通过' if failed == 0 else '❌ 存在失败'}",
        "",
    ]
    if failed:
        lines.append("### 失败明细")
        for kind, v in (result.get("results") or {}).items():
            if not isinstance(v, dict):
                continue
            for r in v.get("results", []):
                if r.get("status") == "failed":
                    lines.append(f"- ❌ {r.get('name')}：{r.get('error', '')[:120]}（分类：{r.get('category', '待确认')}）")
        lines.append("")
    lines.append(f"> 报告产物：`{result.get('artifacts_dir', '')}`")
    lines.append("> 由 AI 测试智能体自动生成，失败分类建议人工复核。")
    return "\n".join(lines)


def feedback_message(db: Session, task_id: int) -> dict:
    """生成通知消息（可对接 IM/邮件/PR API）。"""
    comment = build_pr_comment(db, task_id)
    task = db.get(Task, task_id)
    result = task.result or {} if task else {}
    return {
        "task_id": task_id,
        "type": "pr_comment" if (task and (task.meta or {}).get("trigger")) else "task_finished",
        "content": comment,
        "passed": result.get("passed", 0),
        "failed": result.get("failed", 0),
    }
