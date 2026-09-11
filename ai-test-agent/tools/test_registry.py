"""Agent 注册表 + 查表路由验证（P3）。

A. 注册表：完整性 / mainline 主线顺序 / 前置条件检查
B. dispatch 路由（mock 模式 + 临时 DB）：enabled 跳过、functional_reviewer 停用 → review=skipped
C. web API：seed → list（注册表元信息 ∪ 配置）→ update enabled → list 反映

用法：python3 tools/test_registry.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_TMP = tempfile.mkdtemp(prefix="test_registry_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMP}/t.db"
os.environ["AI_TEST_VECTOR_STORE_DIR"] = f"{_TMP}/chroma"


def _read_commits() -> tuple[str, str]:
    p = _ROOT / "examples" / "whitebox_commits.txt"
    kv = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k.strip()] = v.strip()
    return kv["BASE_COMMIT"], kv["TARGET_COMMIT"]


def main() -> None:
    from agents.registry import build_registry, check_requires
    from core.models import TestTask

    # ---- A. 注册表 ----
    reg = build_registry()
    assert set(reg) == {"orchestrator", "api", "ui", "whitebox", "functional", "functional_reviewer", "verifier"}, \
        f"注册表不完整: {set(reg)}"
    mainline = [s.name for s in reg.values() if s.mainline]
    assert mainline == ["api", "ui", "whitebox", "functional"], f"主线顺序错误: {mainline}"
    assert reg["functional_reviewer"].role == "review", "评审角色应为 B 模型"
    task = TestTask(requirement="x", repo_path="/tmp/r", meta={"openapi_path": "/tmp/o.yaml"})
    assert check_requires(task, "repo_path") and check_requires(task, "openapi")
    assert not check_requires(task, "ui_base_url")
    print(f"[A] 注册表 {len(reg)} 项，主线顺序 {mainline}，前置条件检查 OK")

    # ---- 准备 DB 配置：api 停用 + functional_reviewer 停用 ----
    from web.db import SessionLocal, init_db
    init_db()
    from web.models import AgentConfig
    with SessionLocal() as db:
        for name, spec in reg.items():
            db.add(AgentConfig(name=name, enabled=True, model="mock", harness=spec.harness_default))
        db.commit()
    with SessionLocal() as db:
        db.query(AgentConfig).filter_by(name="api").update({"enabled": False})
        db.query(AgentConfig).filter_by(name="functional_reviewer").update({"enabled": False})
        db.commit()

    # ---- B. dispatch 路由（mock 全流程，scope 含 api + functional） ----
    from agents.orchestrator import Orchestrator
    base, target = _read_commits()
    task = TestTask(
        requirement="电商平台商品发布功能回归 + API 验证：功能测试（登录后可创建/查询商品，覆盖正常/异常/边界/权限场景）+ API 验证商品创建与查询接口",
        base_commit=base, target_commit=target,
        meta={"base_url": "http://127.0.0.1:8100",
              "openapi_path": str(_ROOT / "examples" / "openapi_demo.yaml"),
              "ui_base_url": "http://127.0.0.1:5173"},
    )
    out = Orchestrator(auto_approve=True).run(task)
    assert out["state"] == "DONE", f"状态机未 DONE: {out['state']}"
    results = out["results"]
    assert "api" not in results, "api Agent 已停用，不应出现在结果中"
    func = results.get("functional") or {}
    assert func.get("review", {}).get("overall") == "skipped", \
        f"functional_reviewer 停用后 review 应为 skipped，实际 {func.get('review', {}).get('overall')}"
    assert len(func.get("cases", [])) > 0, "功能用例生成不应为空"
    print(f"[B] dispatch 路由：api 跳过 ✅；functional 生成 {len(func['cases'])} 条；"
          f"评审 skipped ✅（状态 {out['state']}）")

    # ---- C. web API 闭环 ----
    from fastapi.testclient import TestClient
    from web.app import app
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "123456"})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {r.json()['token']}"}
    r = c.post("/api/agents/seed", headers=h)
    assert r.status_code == 200 and r.json()["created"] == 0, "seed 应幂等"
    r = c.get("/api/agents", headers=h)
    agents = r.json()
    assert len(agents) == len(reg), f"列表应含全部注册 Agent，实际 {len(agents)}"
    fr = next(a for a in agents if a["name"] == "functional_reviewer")
    assert fr["role"] == "review" and fr["kind"] == "review" and not fr["mainline"], "评审元信息错误"
    assert fr["enabled"] is False, "评审停用状态应反映"
    assert fr["id"] is not None
    r = c.put(f"/api/agents/{fr['id']}", json={"enabled": True, "model": "gpt-review"}, headers=h)
    assert r.status_code == 200 and r.json()["enabled"] is True
    r = c.get("/api/agents", headers=h)
    fr2 = next(a for a in r.json() if a["name"] == "functional_reviewer")
    assert fr2["enabled"] is True and fr2["model"] == "gpt-review", "update 未生效"
    print(f"[C] web API：seed/list/update 闭环 ✅（{len(agents)} 项，评审可启停可换模型标注）")
    print("\n=== ALL REGISTRY TESTS PASSED ===")


if __name__ == "__main__":
    main()
