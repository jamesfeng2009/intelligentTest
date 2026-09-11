"""AI 测试智能体 —— 一键演示入口。

用法：
    python demo.py                     # 跑全部场景（API + 白盒 + UI 脚本生成）
    python demo.py --api               # 仅 API 测试（自动启动 demo 服务）
    python demo.py --whitebox          # 仅白盒测试（示例仓库 base→target）
    python demo.py --ui                # 仅 UI 脚本生成（不依赖浏览器）
    python demo.py --orchestrator      # 总控 Agent 全流程（需求→计划→执行→报告）

配置 LLM（可选）：
    export AI_TEST_LLM_API_KEY=xxx      # 未配置时自动使用 mock 模式
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).parent
sys.path.insert(0, str(PROJECT))

from core.config import get_settings  # noqa: E402
from core.logging_setup import setup_logging  # noqa: E402

setup_logging()


def _ensure_settings():
    s = get_settings()
    if not s.llm_available:
        print("ℹ️ 未配置 AI_TEST_LLM_API_KEY，使用 mock 模式（确定性生成，可跑通全流程）")
    return s


# ---------------- API 场景 ----------------
def run_api() -> dict:
    from agents.api_tester import APITester, DEFAULT_BASE_URL
    from core.llm import create_llm
    from core.models import TestPlan, TestTask
    from harness.artifacts import ArtifactManager

    print("\n[1/4] API 测试场景")
    # 启动 demo 服务
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "examples.demo_api:app", "--port", "8100", "--log-level", "warning"],
        cwd=str(PROJECT),
    )
    try:
        # 等待服务就绪
        import requests

        for _ in range(30):
            try:
                if requests.get(f"{DEFAULT_BASE_URL}/healthz", timeout=2).ok:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        else:
            print("⚠️ demo 服务启动超时，尝试继续（可能已有实例在跑）")

        task = TestTask(
            requirement="电商商品接口测试：创建商品、查询商品，覆盖正常/异常/边界场景",
            meta={"base_url": DEFAULT_BASE_URL,
                  "openapi_path": str(PROJECT / "examples" / "openapi_demo.yaml")},
        )
        artifacts = ArtifactManager(task.task_id)
        llm = create_llm()
        plan = TestPlan(scope=["api"], strategy="正常/异常/边界", risk_points=["商品创建校验"], scenarios=[], estimates={})
        result = APITester(llm, artifacts).run(task, plan)
        print(f"  {result['summary']}")
        for r in result["results"]:
            mark = "✅" if r["status"] == "passed" else "❌"
            print(f"  {mark} {r['name']}  {r.get('error', '')[:80]}")
        return result
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            proc.kill()


# ---------------- 白盒场景 ----------------
def run_whitebox() -> dict:
    from agents.whitebox_tester import WhiteboxTester
    from core.llm import create_llm
    from core.models import TestPlan, TestTask
    from harness.artifacts import ArtifactManager

    print("\n[2/4] 白盒测试场景")
    commits = {}
    for line in (PROJECT / "examples" / "whitebox_commits.txt").read_text().splitlines():
        k, v = line.split("=", 1)
        commits[k] = v.strip()

    task = TestTask(
        requirement="用户登录模块白盒分析：评估最近一次变更的影响面，生成单元测试",
        repo_path=str(PROJECT / "examples" / "whitebox_demo"),
        base_commit=commits["BASE_COMMIT"],
        target_commit=commits["TARGET_COMMIT"],
    )
    artifacts = ArtifactManager(task.task_id)
    llm = create_llm()
    plan = TestPlan(scope=["whitebox"], strategy="增量分析", risk_points=["登录/权限"], scenarios=[], estimates={})
    result = WhiteboxTester(llm, artifacts).run(task, plan)
    print(f"  {result['summary']}")
    print(f"  变更函数: {[f['function_name'] for f in result.get('changed_functions', [])]}")
    for r in result.get("impact", {}).get("risk", []):
        print(f"  风险[{r['risk_level']}] {r['function']}: {'; '.join(r['reasons'])}")
    for lang, rep in result.get("unit_reports", {}).items():
        print(f"  单测({lang}): 生成 {rep['generated']} 通过 {rep['passed']} 失败 {rep['failed']} 覆盖 {rep.get('coverage')}")
    print(f"  走查意见: {len(result.get('findings', []))} 条")
    return result


def _start_ui_service() -> subprocess.Popen | None:
    """启动 UI 执行服务。有 chromium 时真实浏览器执行；否则自动降级 simulated。"""
    import requests

    env = {**os.environ}
    # 探测 chromium 二进制是否可用（playwright 包已装但浏览器可能未下载）
    probe = subprocess.run(
        ["node", "-e",
         "const {chromium}=require('playwright'); const p=chromium.executablePath(); require('fs').accessSync(p); console.log('OK')"],
        cwd=str(PROJECT / "ui_service"), capture_output=True, text=True, timeout=30,
    )
    if probe.returncode != 0:
        env["AI_TEST_UI_FORCE_SIMULATED"] = "true"
        print("ℹ️ 未检测到 chromium 浏览器二进制，UI 降级 simulated（执行 npx playwright install chromium 可启用真实浏览器执行）")
    else:
        print("✅ 检测到 chromium，UI 使用真实浏览器执行")
    try:
        proc = subprocess.Popen(
            ["node", "server.js"], cwd=str(PROJECT / "ui_service"), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(20):
            try:
                if requests.get("http://127.0.0.1:8399/api/health", timeout=2).status_code == 200:
                    return proc
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        proc.terminate()
        print("⚠️ UI 执行服务启动超时，UI 场景将降级为不可用")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ UI 执行服务启动失败（{e}），UI 场景将降级为不可用")
        return None


DEMO_UI_URL = "http://127.0.0.1:8080/index.html"


def _start_demo_ui() -> subprocess.Popen | None:
    """启动 demo UI 静态页面服务（真实浏览器可访问）。"""
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", "8080", "--directory", str(PROJECT / "examples" / "demo_ui")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        import requests

        for _ in range(20):
            try:
                if requests.get("http://127.0.0.1:8080/index.html", timeout=2).status_code == 200:
                    return proc
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        proc.terminate()
        print("⚠️ demo UI 服务启动超时")
        return None
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ demo UI 服务启动失败（{e}）")
        return None


# ---------------- UI 场景 ----------------
def run_ui() -> dict:
    from agents.ui_tester import UITester
    from core.llm import create_llm
    from core.models import TestPlan, TestTask
    from harness.artifacts import ArtifactManager

    print("\n[3/4] UI 测试场景（真实浏览器执行；未装浏览器时自动降级 simulated）")
    ui_proc = _start_ui_service()
    demo_ui_proc = _start_demo_ui()
    try:
        task = TestTask(requirement="商品发布流程：登录 → 新增商品 → 校验创建成功", meta={"ui_base_url": DEMO_UI_URL})
        artifacts = ArtifactManager(task.task_id)
        llm = create_llm()
        plan = TestPlan(scope=["ui"], strategy="端到端主链路", risk_points=["登录/表单提交"], scenarios=[], estimates={})
        result = UITester(llm, artifacts).run(task, plan)
        print(f"  {result['summary']}")
        print(f"  生成脚本 {len(result.get('script', '').splitlines())} 行（见产物 ui_tester/ui_test_cases.ts）")
        for r in result.get("results", []):
            mark = "✅" if r.get("status") == "passed" else "❌"
            print(f"  {mark} {r.get('name', '')}  {r.get('error', '')[:100]}")
        return result
    finally:
        if ui_proc:
            ui_proc.terminate()
        if demo_ui_proc:
            demo_ui_proc.terminate()


# ---------------- 功能测试场景（A 模型生成 + B 模型评审） ----------------
def run_functional() -> dict:
    from agents.api_tester import parse_openapi
    from agents.functional_reviewer import FunctionalReviewer
    from agents.functional_tester import FunctionalTester
    from core.llm import create_llm
    from core.models import TestPlan, TestTask
    from harness.artifacts import ArtifactManager

    print("\n[5/5] 功能测试场景（A 模型生成用例 → B 模型独立评审）")
    task = TestTask(
        requirement="电商平台商品发布功能回归：用户登录后可以创建商品、查询商品、更新商品，"
                    "覆盖正常/异常/边界/权限场景，且登录失败不得发放凭证",
        meta={"openapi_path": str(PROJECT / "examples" / "openapi_demo.yaml")},
    )
    artifacts = ArtifactManager(task.task_id)
    plan = TestPlan(scope=["functional"], strategy="业务功能场景 + 接口契约",
                    risk_points=["登录鉴权", "商品CRUD"], scenarios=[], estimates={})
    endpoints = parse_openapi(PROJECT / "examples" / "openapi_demo.yaml")

    gen_llm = create_llm(role="main")                 # A 模型
    gen = FunctionalTester(gen_llm, artifacts).run(task, plan)
    print(f"  {gen['summary']}")

    rev_llm = create_llm(role="review")               # B 模型（独立评审）
    rev = FunctionalReviewer(rev_llm, artifacts, generator_llm=gen_llm).run(task, plan, gen["cases"], endpoints)
    print(f"  {rev['summary']}")
    for f in rev["findings"]:
        mark = {"pass": "✅", "fail": "❌", "gap": "⚠️"}.get(f["verdict"], "•")
        print(f"  {mark} [{f['verdict']}/{f['severity']}] {f.get('case_id', '?')} {f['issue'][:90]}")
    print(f"  产物目录: {artifacts.run_dir}")
    return {"gen": gen, "review": rev}


# ---------------- 总控全流程 ----------------
def run_orchestrator() -> dict:
    from agents.orchestrator import Orchestrator
    from core.models import TestTask

    print("\n[4/4] 总控 Agent 全流程")
    commits = {}
    for line in (PROJECT / "examples" / "whitebox_commits.txt").read_text().splitlines():
        k, v = line.split("=", 1)
        commits[k] = v.strip()

    # 拉起 demo API 服务（orchestrator 的 api scope 需要被测服务）
    import requests
    from agents.api_tester import DEFAULT_BASE_URL
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "examples.demo_api:app", "--port", "8100", "--log-level", "warning"],
        cwd=str(PROJECT),
    )
    ui_proc = _start_ui_service()
    demo_ui_proc = _start_demo_ui()
    try:
        for _ in range(30):
            try:
                if requests.get(f"{DEFAULT_BASE_URL}/healthz", timeout=2).ok:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)

        task = TestTask(
            requirement="电商平台商品发布功能回归 + 用户服务登录模块变更分析："
                        "1) 功能测试：登录后可创建/查询/更新商品，覆盖正常/异常/边界/权限场景；"
                        "2) API 验证商品创建/查询接口；3) 白盒分析用户登录模块最近变更的影响面并生成单测",
            repo_path=str(PROJECT / "examples" / "whitebox_demo"),
            base_commit=commits["BASE_COMMIT"],
            target_commit=commits["TARGET_COMMIT"],
            meta={"base_url": "http://127.0.0.1:8100", "openapi_path": str(PROJECT / "examples" / "openapi_demo.yaml"),
                  "ui_base_url": DEMO_UI_URL},
        )
        orchestrator = Orchestrator(auto_approve=True)
        out = orchestrator.run(task)
        print(f"  状态: {out['status']} ({out['state']})")
        for kind, v in out.get("results", {}).items():
            if isinstance(v, dict):
                print(f"  - {kind}: {v.get('summary', '')}")
        print(f"  产物目录: {task.artifacts_dir}")
        print("\n  === 最终报告 ===")
        print(out.get("report", ""))
        return out
    finally:
        proc.send_signal(signal.SIGTERM)
        if ui_proc:
            ui_proc.terminate()
        if demo_ui_proc:
            demo_ui_proc.terminate()


def main() -> None:
    parser = argparse.ArgumentParser(description="AI 测试智能体 Demo")
    parser.add_argument("--api", action="store_true", help="仅 API 测试")
    parser.add_argument("--whitebox", action="store_true", help="仅白盒测试")
    parser.add_argument("--ui", action="store_true", help="仅 UI 脚本生成")
    parser.add_argument("--functional", action="store_true", help="仅功能测试用例生成 + B 模型评审")
    parser.add_argument("--orchestrator", action="store_true", help="总控全流程")
    args = parser.parse_args()

    # UI 场景预设置：simulated 演示模式（在 settings 单例初始化之前）
    if args.ui or args.orchestrator:
        os.environ.setdefault("AI_TEST_UI_SERVICE_ENABLED", "true")

    _ensure_settings()

    if args.api:
        run_api()
    elif args.whitebox:
        run_whitebox()
    elif args.ui:
        run_ui()
    elif args.functional:
        run_functional()
    elif args.orchestrator:
        run_orchestrator()
    else:
        run_api()
        run_whitebox()
        run_ui()
        print("\n✅ Demo 完成。可用 --orchestrator 跑总控全流程（含测试计划与最终报告）")


if __name__ == "__main__":
    main()
