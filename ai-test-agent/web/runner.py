"""任务执行 Runner（对齐 T38：提交→队列→Runner 隔离执行）。

- 按任务类型精确调度：api → APITester；ui → UITester；whitebox → WhiteboxTester；orchestrator → Orchestrator 全流程
- 服务生命周期管理：api demo 服务 / ui demo 页面 + ui_service 子进程按需拉起、随任务退出
- 结果写入 DB（Task.status / Report），产物（报告/截图）进对象存储
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent

# 任务类型 → 默认 scope / 策略 / 风险点
_TYPE_PLAN: dict[str, dict] = {
    "api": {"scope": ["api"], "strategy": "正常/异常/边界", "risk_points": ["接口契约", "参数校验"]},
    "ui": {"scope": ["ui"], "strategy": "端到端主链路", "risk_points": ["核心业务链路", "页面可用性"]},
    "whitebox": {"scope": ["whitebox"], "strategy": "增量分析", "risk_points": ["变更影响面", "登录/权限"]},
    "functional": {"scope": ["functional"], "strategy": "业务功能场景 + 接口契约", "risk_points": ["登录鉴权", "商品CRUD"]},
    "security": {"scope": ["security"], "strategy": "安全用例生成 + 鉴权缺口静态扫描", "risk_points": ["越权", "注入", "凭证安全"]},
    "orchestrator": {"scope": ["api", "ui", "whitebox", "functional", "security"], "strategy": "正常/异常/边界 + 变更增量 + 功能评审 + 安全审查",
                     "risk_points": ["核心链路回归", "变更影响面", "需求覆盖度", "鉴权缺口"]},
}


def _wait_ready(url: str, timeout: float = 20.0) -> bool:
    import requests

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=2).ok:
                return True
        except Exception:  # noqa: BLE001
            time.sleep(0.4)
    return False


def _start_demo_api() -> subprocess.Popen | None:
    import os

    if os.environ.get("AI_TEST_API_BASE_URL"):
        return None  # 外部被测服务
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "examples.demo_api:app", "--port", "8100", "--log-level", "warning"],
        cwd=str(PROJECT),
    )


def _start_demo_ui() -> subprocess.Popen | None:
    return subprocess.Popen(
        [sys.executable, "-m", "http.server", "8080", "--directory", "examples/demo_ui"],
        cwd=str(PROJECT),
    )


def _start_ui_service() -> subprocess.Popen | None:
    node = Path(PROJECT) / "ui_service"
    if not (node / "node_modules").exists():
        return None
    # 不覆盖 env：继承父进程 PATH（sandbox 下 node 在 PATH 中）
    return subprocess.Popen(
        ["node", "server.js"], cwd=str(node),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def run_task(requirement: str, task_type: str, meta: dict | None = None) -> dict:
    """同步执行一个任务，返回统一结果 dict。被 queue worker 在独立线程中调用。"""
    from core.llm import create_llm
    from core.models import TestPlan, TestTask
    from harness.artifacts import ArtifactManager

    meta = meta or {}
    plan_spec = _TYPE_PLAN.get(task_type, _TYPE_PLAN["orchestrator"])

    api_proc = ui_proc = ui_service_proc = None
    try:
        if task_type in ("api", "orchestrator"):
            api_proc = _start_demo_api()
            base_url = meta.get("base_url") or "http://127.0.0.1:8100"
            if api_proc:
                _wait_ready(f"{base_url}/healthz")

        if task_type in ("ui", "orchestrator"):
            ui_proc = _start_demo_ui()
            ui_service_proc = _start_ui_service()
            ui_base = meta.get("ui_base_url") or "http://127.0.0.1:8080/index.html"
            meta["ui_base_url"] = ui_base

        task = TestTask(
            requirement=requirement,
            repo_path=meta.get("repo_path") or str(PROJECT / "examples" / "whitebox_demo"),
            base_commit=meta.get("base_commit"),
            target_commit=meta.get("target_commit"),
            meta={
                "base_url": meta.get("base_url") or "http://127.0.0.1:8100",
                "openapi_path": str(PROJECT / "examples" / "openapi_demo.yaml"),
                "ui_base_url": meta.get("ui_base_url"),
            },
        )

        if task_type == "orchestrator":
            from agents.orchestrator import Orchestrator

            out = Orchestrator(auto_approve=True).run(task)
            return _normalize_orchestrator_out(out, task)
        else:
            llm = create_llm()
            artifacts = ArtifactManager(task.task_id)
            plan = TestPlan(scope=plan_spec["scope"], strategy=plan_spec["strategy"],
                            risk_points=plan_spec["risk_points"], scenarios=[], estimates={})
            if task_type == "api":
                from agents.api_tester import APITester

                result = APITester(llm, artifacts).run(task, plan, spec_path=meta.get("openapi_path"),
                                                       base_url=meta.get("base_url") or "http://127.0.0.1:8100")
            elif task_type == "ui":
                from agents.ui_tester import UITester

                result = UITester(llm, artifacts).run(task, plan, base_url=meta.get("ui_base_url"))
            elif task_type == "functional":
                from agents.api_tester import parse_openapi
                from agents.functional_reviewer import FunctionalReviewer
                from agents.functional_tester import FunctionalTester
                from core.llm import create_llm

                endpoints = parse_openapi(meta.get("openapi_path") or str(PROJECT / "examples" / "openapi_demo.yaml"))
                gen = FunctionalTester(llm, artifacts).run(task, plan)
                review_llm = create_llm(role="review")   # B 模型
                rev = FunctionalReviewer(review_llm, artifacts, generator_llm=llm).run(task, plan, gen["cases"], endpoints)
                result = {"summary": f"{gen['summary']}；{rev['summary']}", "results": [],
                          "cases": gen["cases"], "review": rev, "features": gen.get("features", [])}
            elif task_type == "security":
                from agents.security_tester import SecurityTester

                result = SecurityTester(llm, artifacts).run(task, plan,
                                                            spec_path=meta.get("openapi_path") or str(PROJECT / "examples" / "openapi_demo.yaml"))
            else:  # whitebox
                from agents.whitebox_tester import WhiteboxTester

                result = WhiteboxTester(llm, artifacts).run(task, plan)
            return _normalize_tester_out(result, task)
    finally:
        _stop(api_proc)
        _stop(ui_proc)
        _stop(ui_service_proc)


def _normalize_tester_out(result: dict, task: TestTask) -> dict:
    """单线 Tester 结果 → 统一结构。"""
    results = result.get("results", [])
    passed = sum(1 for r in results if r.get("status") == "passed")
    failed = sum(1 for r in results if r.get("status") == "failed")
    total = len(results)
    return {
        "status": "done" if failed == 0 else "failed",
        "summary": result.get("summary", f"{passed}/{total} 通过"),
        "passed": passed,
        "failed": failed,
        "total": total,
        "passed_rate": round(passed / total * 100, 2) if total else 0.0,
        "results": results,
        "detail": {k: v for k, v in result.items() if k not in ("results",)},
        "artifacts_dir": getattr(task, "artifacts_dir", "") or "",
    }


def _normalize_orchestrator_out(out: dict, task: TestTask) -> dict:
    """Orchestrator 结果 → 统一结构。"""
    results: dict = out.get("results", {})
    agg = {"passed": 0, "failed": 0, "total": 0}
    for kind, v in results.items():
        if not isinstance(v, dict):
            continue
        items = v.get("results", [])
        passed = sum(1 for r in items if r.get("status") == "passed")
        failed = sum(1 for r in items if r.get("status") == "failed")
        agg["passed"] += passed
        agg["failed"] += failed
        agg["total"] += len(items)
    total = agg["total"]
    return {
        "status": "done" if out.get("status") == "DONE" else ("failed" if out.get("status") == "FAILED" else out.get("status", "done")),
        "summary": f"api/ui/whitebox 共 {agg['passed']}/{total} 通过" if total else out.get("status", "done"),
        "passed": agg["passed"],
        "failed": agg["failed"],
        "total": total,
        "passed_rate": round(agg["passed"] / total * 100, 2) if total else 0.0,
        "results": results,
        "detail": {"report": out.get("report", ""), "state": out.get("state", "")},
        "artifacts_dir": getattr(task, "artifacts_dir", "") or "",
        "trace": out.get("trace", []),
    }


def persist_report(db, task_row, normalized: dict, report_type: str) -> None:
    """把执行结果写入 DB：任务状态 + 报告 + 指标。"""
    from sqlalchemy.orm import Session

    from .models import MetricSnapshot, Report

    task_row.status = "done" if normalized["status"] == "done" else "failed"
    task_row.result = normalized
    from datetime import datetime

    task_row.finished_at = datetime.now()

    report = Report(
        task_id=task_row.id,
        report_type=report_type,
        summary=normalized.get("summary", ""),
        passed=normalized.get("passed", 0),
        failed=normalized.get("failed", 0),
        total=normalized.get("total", 0),
        passed_rate=normalized.get("passed_rate", 0.0),
        detail=normalized.get("detail", {}),
        artifact_paths=[normalized.get("artifacts_dir", "")],
    )
    db.add(report)

    # 指标快照：通过率 + 回归时长（分钟）
    from datetime import datetime as _dt

    if task_row.started_at:
        minutes = max(1, round((_dt.now() - task_row.started_at).total_seconds() / 60, 2))
        db.add(MetricSnapshot(project_id=task_row.project_id, name="回归时长", value=minutes, unit="min", period="task"))
    db.add(MetricSnapshot(project_id=task_row.project_id, name="通过率", value=normalized.get("passed_rate", 0.0), unit="%", period="task"))
    db.flush()
