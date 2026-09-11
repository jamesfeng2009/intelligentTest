"""P0 七项低成本加固专项验证：A2 / A4 / A15 / A18 / A8 / C2 / C8。

用法：python3 tools/verify_p0_gaps.py
覆盖：
  A2  目录驱动提取：章节↔是否已提取完成度对照（含缺口标记）
  A4  源约束强制：验收标准/来源缺失 → 强制产出风险；模糊措辞兜底
  A15 预估用例数：LLM 提供则保留，缺失则确定性兜底估算
  A18 防幻觉执行层：多语句拆行（禁止合并步骤）+ 关键步骤保全校验（清空/重置）
  A8  前后端工程识别：package.json(pom/go.mod) 特征 → frontend/backend/fullstack/unknown
  C2  禁止行为清单：prompt 声明 + 运行时护栏命中检测
  C8  错误对策映射：错误特征 → 类型 → 对策注入（timeout/selector/network/business）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_TMPDIR = tempfile.mkdtemp(prefix="verify_p0gaps_")

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


class FakeLLM:
    """确定性 Fake：Pass1 返回含 est_cases 的条目；RQ-002 缺验收标准、RQ-003 缺来源（测 A4）。"""

    name = "fake"

    def chat(self, system: str, user: str, json_mode: bool = False, **kwargs):
        if "requirement_parse_pass1" in user:
            return {
                "summary": "商品发布",
                "items": [
                    {"id": "RQ-001", "title": "商品创建", "desc": "创建商品", "acceptance": ["201含商品信息", "缺字段422"],
                     "priority": "P0", "source": "3.1 商品创建", "est_cases": 6},
                    {"id": "RQ-002", "title": "商品删除", "desc": "删除商品", "acceptance": [],
                     "priority": "P1", "source": "3.2 商品删除"},
                    {"id": "RQ-003", "title": "商品导出", "desc": "导出商品列表", "acceptance": ["200含CSV"],
                     "priority": "P2", "source": ""},
                ],
            }
        if "requirement_parse_pass2" in user:
            return {"items": [
                {"id": "RQ-001", "rules": ["价格必须大于0"], "constraints": ["名称长度≤100"]},
                {"id": "RQ-002", "rules": [], "constraints": []},
                {"id": "RQ-003", "rules": [], "constraints": []},
            ]}
        if "requirement_parse_pass3" in user:
            return {"items": [
                {"id": "RQ-001", "involved_endpoints": [{"method": "POST", "path": "/api/v1/products"}],
                 "upstream_systems": ["商家后台"], "downstream_systems": ["商品中心"], "data_compat_rules": []},
                {"id": "RQ-002", "involved_endpoints": [], "upstream_systems": [], "downstream_systems": [], "data_compat_rules": []},
                {"id": "RQ-003", "involved_endpoints": [], "upstream_systems": [], "downstream_systems": [], "data_compat_rules": []},
            ]}
        if "requirement_parse_pass4" in user:
            return {"risks": [], "edge_cases": [], "anomalies": []}
        return {"items": []}

    def chat_json(self, system: str, user: str, **kwargs):
        return self.chat(system, user, True)


class CleanLLM(FakeLLM):
    """无缺陷条目（验收标准/来源齐全）——用于验证模糊措辞兜底分支。"""

    def chat(self, system: str, user: str, json_mode: bool = False, **kwargs):
        if "requirement_parse_pass1" in user:
            return {"summary": "x", "items": [
                {"id": "RQ-001", "title": "订单管理", "desc": "订单管理", "acceptance": ["200正常"],
                 "priority": "P0", "source": "1.1 订单管理", "est_cases": 3}]}
        if "requirement_parse_pass2" in user:
            return {"items": [{"id": "RQ-001", "rules": [], "constraints": []}]}
        if "requirement_parse_pass3" in user:
            return {"items": [{"id": "RQ-001", "involved_endpoints": [], "upstream_systems": [], "downstream_systems": [], "data_compat_rules": []}]}
        if "requirement_parse_pass4" in user:
            return {"risks": [], "edge_cases": [], "anomalies": []}
        return {"items": []}


REQUIREMENT = """# 商品管理需求
## 3. 商品模块
### 3.1 商品创建
创建商品需填写名称与价格，价格必须大于 0。
### 3.2 商品删除
支持删除商品。
### 3.3 商品导出
导出商品列表为 CSV。
"""

ENDPOINTS = [{"method": "POST", "path": "/api/v1/products"},
             {"method": "DELETE", "path": "/api/v1/products/{id}"},
             {"method": "GET", "path": "/api/v1/products/export"}]


def test_fuzzy_fallback() -> None:
    """A4 模糊措辞兜底分支：条目无缺陷但需求含模糊措辞 → 强制复核风险。"""
    from knowledge.requirement_parser import parse_requirement
    res = parse_requirement("# 订单\n## 1.1 订单管理\n实现订单管理等适当功能，尽快上线", CleanLLM(), endpoints=[])
    check("A4 模糊措辞且无其它风险 → 强制复核风险（CleanLLM 直测）",
          any("模糊措辞" in r for r in res.risks), str(res.risks))


def main() -> None:
    # ================= A2 / A4 / A15（需求解析程序级强制） =================
    print("\n[A2/A4/A15] 目录驱动提取 / 源约束强制 / 预估用例数")
    from knowledge.requirement_parser import parse_requirement
    res = parse_requirement(REQUIREMENT, FakeLLM(), endpoints=ENDPOINTS)

    # A2：章节完成度对照
    sec_map = {s["title"]: s for s in res.sections}
    check("A2 章节完成度：已提取章节标记 extracted=True",
          sec_map.get("3.1 商品创建", {}).get("extracted") is True and "RQ-001" in sec_map.get("3.1 商品创建", {}).get("item_ids", []))
    check("A2 章节完成度：来源匹配章节标记 extracted=True",
          sec_map.get("3.2 商品删除", {}).get("extracted") is True and "RQ-002" in sec_map["3.2 商品删除"].get("item_ids", []))
    check("A2 章节完成度：无条目覆盖章节标记未提取",
          any(s.get("extracted") is False and s.get("gap") for s in res.sections), str([s["title"] for s in res.sections if not s.get("extracted")]))

    # A4：源约束强制
    risk_text = "；".join(res.risks)
    check("A4 验收标准缺失 → 强制风险", "RQ-002" in risk_text and "验收标准缺失" in risk_text)
    check("A4 来源章节缺失 → 强制风险", "RQ-003" in risk_text and "来源章节缺失" in risk_text)
    # 模糊措辞兜底：新需求含"等/适当"且无风险时应输出复核风险
    res2 = parse_requirement("实现订单管理等适当功能，尽快上线", FakeLLM(), endpoints=[])
    check("A4 模糊措辞兜底 → 复核风险", any("模糊措辞" in r for r in res2.risks) or any("【源约束】" in r for r in res2.risks),
          str(res2.risks[:2]))
    test_fuzzy_fallback()

    # A15：预估用例数
    m = {it.id: it for it in res.items}
    check("A15 LLM 提供的 est_cases 保留", m["RQ-001"].est_cases == 6, str(m["RQ-001"].est_cases))
    check("A15 缺失 est_cases 确定性兜底（≥1）",
          m["RQ-002"].est_cases >= 1 and m["RQ-003"].est_cases >= 1, f"RQ-002={m['RQ-002'].est_cases}, RQ-003={m['RQ-003'].est_cases}")
    check("A15 est_cases 落盘到 to_dict", m["RQ-001"].to_dict()["est_cases"] == 6)

    # ================= A18 防幻觉执行层 =================
    print("\n[A18] 防幻觉执行层（多语句拆行 + 关键步骤保全）")
    from agents.ui_tester import UITester
    script, step_map = UITester._ensure_step_comments(
        "await agent.aiInput('用户名', 'admin'); await agent.aiTap('登录');\nawait agent.aiAssert('登录成功');")
    check("A18 多语句行拆分为独立步骤", len(step_map) == 3 and step_map[0]["step"] == "1.1" and step_map[1]["step"] == "1.2",
          str([s["step"] for s in step_map]))
    # 关键步骤保全：需求提到"清空输入框"
    from core.models import TestPlan
    plan = TestPlan(scope=["ui"], strategy="x", risk_points=[], scenarios=[], estimates={})
    w1 = UITester._validate_step_completeness(plan, "登录前需要清空输入框再输入", step_map)
    check("A18 需求要求清空而脚本缺失 → 告警", any("清空" in w for w in w1), str(w1))
    script2, step_map2 = UITester._ensure_step_comments("await agent.aiInput('清空用户名输入框', '');\nawait agent.aiInput('用户名', 'admin');")
    w2 = UITester._validate_step_completeness(plan, "登录前需要清空输入框再输入", step_map2)
    check("A18 脚本含清空步骤 → 无告警", not any("清空" in w for w in w2), str(w2))

    # ================= A8 前后端工程识别 =================
    print("\n[A8] 前后端工程识别")
    from adapters.project_detect import detect_project_type
    front = Path(_TMPDIR) / "front"; front.mkdir()
    (front / "package.json").write_text(json.dumps({"dependencies": {"react": "^18", "vite": "^5"}}), encoding="utf-8")
    (front / "src").mkdir(); (front / "src" / "main.tsx").write_text("", encoding="utf-8")
    back = Path(_TMPDIR) / "back"; back.mkdir()
    (back / "pom.xml").write_text("<project><artifactId>svc</artifactId></project>", encoding="utf-8")
    (back / "src" / "main" / "java").mkdir(parents=True)
    full = Path(_TMPDIR) / "full"; full.mkdir()
    (full / "package.json").write_text(json.dumps({"dependencies": {"vue": "^3"}}), encoding="utf-8")
    (full / "go.mod").write_text("module svc", encoding="utf-8")
    empty = Path(_TMPDIR) / "empty"; empty.mkdir()
    check("A8 前端工程识别（react+vite+main.tsx）", detect_project_type(front)["type"] == "frontend", str(detect_project_type(front)))
    check("A8 后端工程识别（pom.xml+java）", detect_project_type(back)["type"] == "backend")
    check("A8 全栈识别（package.json+go.mod）", detect_project_type(full)["type"] == "fullstack")
    check("A8 空目录 → unknown", detect_project_type(empty)["type"] == "unknown")
    det = detect_project_type(front)
    check("A8 证据可追溯（非空）", len(det["evidence"]) >= 1, str(det["evidence"]))

    # ================= C2 禁止行为清单 =================
    print("\n[C2] 禁止行为清单")
    from harness.guards import FORBIDDEN_BEHAVIORS, check_forbidden
    check("C2 清单 ≥ 6 条", len(FORBIDDEN_BEHAVIORS) >= 6, str(len(FORBIDDEN_BEHAVIORS)))
    check("C2 命中禁止行为（改需求工单）→ False", check_forbidden("尝试直接修改需求工单") is False)
    check("C2 命中禁止行为（跳过验证报告成功）→ False", check_forbidden("跳过验证阶段直接报告成功") is False)
    check("C2 合法行为（生成测试计划）→ True", check_forbidden("生成测试计划并调度子 Agent") is True)
    orch_prompt = (Path(_ROOT) / "prompts" / "orchestrator.md").read_text(encoding="utf-8")
    check("C2 prompt 声明禁止行为清单", "# 禁止行为" in orch_prompt and "禁止直接修改需求工单" in orch_prompt)

    # ================= C8 错误对策映射 =================
    print("\n[C8] 错误对策映射")
    r1 = UITester._classify_error("Element not found: 登录按钮")
    check("C8 selector_not_found 分类可自愈", r1[0] is True and r1[1] == "selector_not_found" and len(r1[2]) >= 1)
    r2 = UITester._classify_error("TimeoutError: waiting for locator 5s")
    check("C8 timeout 分类可自愈", r2[0] is True and r2[1] == "timeout")
    r3 = UITester._classify_error("net::ERR_CONNECTION_RESET")
    check("C8 network 分类可自愈", r3[0] is True and r3[1] == "network")
    r4 = UITester._classify_error("Assertion failed: 金额计算错误 100 != 90")
    check("C8 业务断言失败 → 不可自愈（功能缺陷）", r4[0] is False and r4[1] == "business_assert")
    injected = UITester._apply_countermeasure("await agent.aiTap('确定');", "timeout")
    check("C8 timeout 对策注入 aiWaitFor 稳定化", "aiWaitFor" in injected and "Step 0.0" in injected)
    same = UITester._apply_countermeasure("await agent.aiTap('确定');", "business_assert")
    check("C8 业务错误不注入对策", same == "await agent.aiTap('确定');")
    check("C8 兼容入口 _recoverable", UITester._recoverable("timeout 超时") is True and UITester._recoverable("金额错误") is False)

    print("\n" + ("=== ALL P0 GAPS VERIFY PASSED ===" if not FAILED else f"=== {len(FAILED)} FAILED: {FAILED} ==="))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
