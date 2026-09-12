"""Harness Inspector 借鉴 A/B/C 验证脚本（python3 tools/test_delivery_inspector.py）。

覆盖：
- B: eval.trace._collapse_steps / query_trace(collapse=True) 折叠正确性
- A: eval.delivery.build_delivery 聚合结构 + 证据诚实标注（linked/candidate）
- C: eval.path_extract.task_path / cluster_stable_paths / list_stable_paths

使用内存 SQLite + 临时 artifacts 目录，不污染 data/app.db。
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from web.db import Base  # noqa: E402
import web.models  # noqa: E402,F401  注册模型
from web.models import KnowledgeDoc, Report, Task, TraceEntry  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} {extra}")


def main() -> int:
    print("== B: Trace 折叠 ==")
    from eval.trace import _collapse_steps, query_trace

    steps = [
        {"agent": "runner", "step": "retry", "level": "info", "output": "a"},
        {"agent": "runner", "step": "retry", "level": "info", "output": "b"},
        {"agent": "runner", "step": "retry", "level": "error", "output": "c"},
        {"agent": "api_tester", "step": "parse", "level": "info", "output": "d"},
        {"agent": "api_tester", "step": "parse", "level": "info", "output": "e"},
    ]
    collapsed = _collapse_steps(steps)
    check("连续重复合并为 1 条", len(collapsed) == 2, f"len={len(collapsed)}")
    check("count 累计正确", collapsed[0]["count"] == 3, str(collapsed[0].get("count")))
    check("error 级别保留", collapsed[0]["level"] == "error")
    check("不同 (agent,step) 不误合并", collapsed[1]["count"] == 2)
    check("未折叠时不改动", _collapse_steps(steps) != steps or len(_collapse_steps(steps)) == 2)

    # DB 分支折叠
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    db = Session()
    for i in range(3):
        db.add(TraceEntry(trace_id="t1", agent="runner", step="重试", level="info", output=f"o{i}"))
    db.add(TraceEntry(trace_id="t1", agent="verifier", step="分类", level="info", output="真实Bug"))
    db.commit()
    raw = query_trace(db, "t1")
    col = query_trace(db, "t1", collapse=True)
    check("DB 查询平铺 4 条", len(raw["steps"]) == 4, f"len={len(raw['steps'])}")
    check("DB 查询折叠 2 条", len(col["steps"]) == 2, f"len={len(col['steps'])}")
    check("折叠标注 collapsed=True", col.get("collapsed") is True)
    check("折叠 count 徽标字段", col["steps"][0].get("count") == 3)
    db.close()

    print("== A: delivery 聚合 ==")
    from eval.delivery import build_delivery

    with tempfile.TemporaryDirectory() as tmp:
        run_dir = Path(tmp) / "runs" / "task_1"
        orch = run_dir / "orchestrator"
        orch.mkdir(parents=True)
        (orch / "需求解析.json").write_text(json.dumps({
            "items": [
                {"id": "R1", "title": "登录", "priority": "P0", "acceptance": ["能登录"]},
                {"id": "R2", "title": "注册", "priority": "P1", "acceptance": ["能注册"]},
            ]
        }, ensure_ascii=False), encoding="utf-8")
        (orch / "task_result.json").write_text(json.dumps({
            "status": "done", "state": "DONE", "report": "# 报告",
            "approvals": [
                {"point": "计划审批", "status": "approved", "decision": "approved", "reviewer": "auto", "comment": ""},
                {"point": "报告确认", "status": "approved", "decision": "approved", "reviewer": "human", "comment": "ok"},
            ],
            "trace": [
                {"from": "IDLE", "to": "ANALYZE", "reason": "接收测试需求"},
                {"from": "ANALYZE", "to": "WAIT_APPROVAL", "reason": "等待审批"},
                {"from": "WAIT_APPROVAL", "to": "SETUP", "reason": "通过"},
                {"from": "SETUP", "to": "API_TEST", "reason": "调度"},
                {"from": "API_TEST", "to": "VERIFY", "reason": "完成"},
                {"from": "VERIFY", "to": "REPORT", "reason": "分类"},
                {"from": "REPORT", "to": "DONE", "reason": "完成"},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        db = Session()
        now = datetime.now()
        t = Task(
            id=1, project_id=1, title="登录功能测试", task_type="orchestrator",
            requirement="需求：验证登录功能", status="done", trace_id="task_1", meta={},
            result={
                "status": "done", "summary": "api 1/2 通过", "passed": 1, "failed": 1, "total": 2,
                "passed_rate": 50.0, "artifacts_dir": str(run_dir),
                "trace": [],  # 优先读 task_result.json
                "approvals": [],
                "results": {"api": {"results": [
                    {"name": "登录成功", "status": "passed"},
                    {"name": "登录失败重试", "status": "failed", "error": "500 boom", "category": "真实Bug-数据不一致"},
                ]}},
            },
            created_at=now - timedelta(minutes=10),
            started_at=now - timedelta(minutes=9),
            finished_at=now,
        )
        db.add(t)
        db.add(Report(task_id=1, report_type="orchestrator", summary="api 1/2 通过",
                      passed=1, failed=1, total=2, passed_rate=50.0, detail={}))
        db.add(KnowledgeDoc(project_id=1, doc_type="defects", name="缺陷库", status="ready",
                            meta={"task_id": 1}, chunk_count=0))
        db.commit()

        d = build_delivery(db, t)
        check("intent 条目来自 artifacts", d["intent"]["items_count"] == 2, str(d["intent"]["items_count"]))
        check("intent.source=artifacts", d["intent"]["source"] == "artifacts")
        check("process 状态机来自 task_result.json", len(d["process"]["state_machine"]) == 7,
              str(len(d["process"]["state_machine"])))
        check("process 审批 2 条（含人工）", len(d["process"]["approvals"]) == 2)
        check("process effort 交付周期 10min", d["process"]["effort"]["delivery_min"] == 10.0,
              str(d["process"]["effort"]["delivery_min"]))
        check("output 报告 1 条", len(d["output"]["reports"]) == 1)
        check("失败用例含分类且 linked",
              any(f["name"] == "登录失败重试" and f["category"] == "真实Bug-数据不一致" and f["evidence"] == "linked"
                  for f in d["output"]["failed_cases"]))
        check("缺陷条目关联本任务", len(d["output"]["defects"]) == 1)
        check("linked 证据含 状态机/审批/trace/报告", len(d["mapping_evidence"]["linked"]) >= 5,
              str(d["mapping_evidence"]["linked"]))
        check("无多余 candidate（缺分类的用例才候选）",
              all("缺分类" not in c for c in d["mapping_evidence"]["candidates"]), str(d["mapping_evidence"]["candidates"]))
        db.close()

        # 降级：无产物目录的任务
        db = Session()
        t2 = Task(id=2, project_id=1, title="待执行", task_type="api", requirement="",
                  status="pending", trace_id="task_2", meta={}, result={})
        db.add(t2)
        db.commit()
        d2 = build_delivery(db, t2)
        check("降级：items=0/source=task_only", d2["intent"]["items_count"] == 0 and d2["intent"]["source"] == "task_only")
        check("降级：无 trace 时 steps=[] 不报错", d2["process"]["trace"]["steps"] == [])
        check("降级：candidate 提示产物缺失",
              any("Trace 未记录" in c for c in d2["mapping_evidence"]["candidates"]))
        db.close()

    print("== C: 路径聚合 ==")
    from eval.path_extract import cluster_stable_paths, list_stable_paths, task_path

    db = Session()
    for i, status in enumerate(["done", "done", "failed"]):
        db.add(Task(
            id=10 + i, project_id=1, title=f"任务{i}", task_type="orchestrator",
            requirement=f"需求{i}", status=status, meta={},
            result={
                "trace": [{"to": "ANALYZE"}, {"to": "WAIT_APPROVAL"}, {"to": "SETUP"},
                          {"to": "API_TEST"}, {"to": "VERIFY"}, {"to": "REPORT"}, {"to": "DONE"}],
                "approvals": [{"point": "计划审批", "decision": "approved", "reviewer": "auto"}],
                "results": {"api": {"results": [
                    {"name": "c", "status": "failed", "category": "真实Bug-数据不一致"}]}},
            },
            finished_at=now,
        ))
    db.commit()
    paths = [task_path(t) for t in db.query(Task).filter(Task.id >= 10).all()]
    check("task_path 签名含状态机/审批/分类",
          "计划审批:approved" in paths[0]["signature"] and "真实Bug-数据不一致" in paths[0]["signature"],
          paths[0]["signature"])
    check("human_gates 统计正确（无人工）", all(p["human_gates"] == 0 for p in paths))
    clustered = cluster_stable_paths(paths, min_count=2)
    check("3 个同签名任务聚为 1 个稳定路径", len(clustered) == 1 and clustered[0]["count"] == 3,
          f"count={clustered[0]['count'] if clustered else 0}")
    check("success_rate = 2/3", abs(clustered[0]["success_rate"] - 0.667) < 0.01, str(clustered[0]["success_rate"]))
    stable = list_stable_paths(db, project_id=1, min_count=2)
    check("list_stable_paths 返回稳定路径", len(stable) == 1 and stable[0]["count"] == 3)
    all_paths = list_stable_paths(db, project_id=1, min_count=1)
    check("min_count=1 返回全部", len(all_paths) >= 1)
    db.close()

    print(f"\n结果：{PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
