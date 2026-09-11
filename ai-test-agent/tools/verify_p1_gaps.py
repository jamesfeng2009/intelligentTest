"""P1 五项高价值能力专项验证：B12 / A3 / A12 / A10 / A17。

用法：python3 tools/verify_p1_gaps.py
使用独立临时数据库与临时 git 仓库，不污染历史 data/app.db。
覆盖：
  B12 知识库注入用例生成：build_case_context 三类知识（prd/cases/checklist）检索组装
  A3  分层多趟提取：FakeLLM 按 4 趟返回，验证合并去重与 pass_report
  A12 动态代码模式检查：Job/MQ/延迟消息模式识别 + 专属检查清单
  A10 契约变化检测：临时 git 仓库签名变更 / 枚举新增 / 路由变化
  A17 结构化映射+双向追溯：步骤序号化 / // Step 注释强制 / step_map / pytest Step 注释
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_TMPDIR = tempfile.mkdtemp(prefix="verify_p1gaps_")
os.environ["DATABASE_URL"] = f"sqlite:///{_TMPDIR}/verify.db"
os.environ["AI_TEST_VECTOR_STORE_DIR"] = f"{_TMPDIR}/chroma"

PROJECT_ID = 888901

REQUIREMENT = """# 电商平台需求 v1.2

## 3. 用户中心
### 3.1 用户注册
新用户可注册：用户名密码不能为空，用户名唯一。
### 3.2 用户登录
用户凭用户名密码登录，登录成功返回 token；登录失败不得发放凭证。

## 4. 商品模块
### 4.1 商品发布
创建商品需填写名称与价格，价格必须大于 0，名称长度不超过 100；成功返回 201 与商品信息。
### 4.2 商品查询
按 id 查询商品，存在返回 200 与详情，不存在返回 404。

## 5. 权限模型
未携带有效凭证访问受保护接口应返回 401/403，不得绕过。
"""

ENDPOINTS = [
    {"method": "POST", "path": "/api/v1/register"},
    {"method": "POST", "path": "/api/v1/login"},
    {"method": "POST", "path": "/api/v1/products"},
    {"method": "GET", "path": "/api/v1/products/{id}"},
    {"method": "PUT", "path": "/api/v1/products/{id}"},
]

FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "✅" if cond else "❌"
    print(f"{mark} {name}" + (f" — {detail}" if detail else ""))
    if not cond:
        FAILED.append(name)


# ============ FakeLLM：按趟次标记返回对应趟次的输出（真实多趟行为） ============
class FakeLLM:
    """确定性 Fake：识别 requirement_parse_passN 标记，返回该趟专属 schema，用于验证合并。"""

    name = "fake"

    def chat(self, system: str, user: str, json_mode: bool = False, **kwargs):
        import re
        items_p1 = [
            {"id": "RQ-001", "title": "用户登录", "desc": "登录获取凭证", "acceptance": ["返回200含token", "错误密码401"],
             "priority": "P0", "source": "3.2 用户登录"},
            {"id": "RQ-002", "title": "商品创建", "desc": "创建商品", "acceptance": ["201含商品信息", "缺字段422"],
             "priority": "P0", "source": "4.1 商品发布"},
        ]
        if "requirement_parse_pass1" in user:
            return {"summary": "用户登录与商品发布", "items": items_p1}
        if "requirement_parse_pass2" in user:
            return {"items": [
                {"id": "RQ-001", "rules": ["登录失败不得发放凭证"], "constraints": ["用户名长度≤200"]},
                {"id": "RQ-002", "rules": ["价格必须大于0", "名称长度不超过100"], "constraints": ["价格不能为负数"]},
            ]}
        if "requirement_parse_pass3" in user:
            return {"items": [
                {"id": "RQ-001", "involved_endpoints": [{"method": "POST", "path": "/api/v1/login"}],
                 "upstream_systems": ["用户中心"], "downstream_systems": ["凭证服务"],
                 "data_compat_rules": ["token 格式兼容旧版"]},
                {"id": "RQ-002", "involved_endpoints": [{"method": "POST", "path": "/api/v1/products"}],
                 "upstream_systems": ["商家后台"], "downstream_systems": ["商品中心"],
                 "data_compat_rules": ["价格字段从 int 升级为 decimal，需兼容旧数据"]},
            ]}
        if "requirement_parse_pass4" in user:
            return {"risks": ["需求未明确登录失败重试策略", "商品查询分页规则缺失"],
                    "edge_cases": ["用户名超长", "价格为0与负数"], "anomalies": ["失败应返回明确错误码"]}
        return {"items": [], "summary": "fallback"}

    def chat_text(self, system: str, user: str, **kwargs):
        return ""

    def chat_json(self, system: str, user: str, **kwargs):
        out = self.chat(system, user, True)
        return out if isinstance(out, dict) else {"text": str(out)}


def seed_case_knowledge(db, embedder_holder: dict) -> None:
    """模拟 /knowledge/upload 入库三类知识（prd 操作手册 / cases 基线用例 / checklist 上线检查清单）。"""
    from knowledge.chunking import split_document_structured, tokenize
    from knowledge.embeddings import TfidfEmbedder, create_embedder
    from knowledge.vectorstore import create_vectorstore
    from web.models import KnowledgeChunk, KnowledgeDoc

    docs = [
        ("prd", "商品模块操作手册",
         "# 商品发布操作手册\n## 发布流程\n商家登录后进入商品管理，点击新增商品，填写名称与价格，提交后进入审核。\n## 审核设置\n规格项最多设置5个。"),
        ("cases", "商品历史基线用例",
         "# 商品管理历史用例\n## 商品创建\n创建合法商品成功：POST /api/v1/products，返回201。\n缺必填字段：返回422。"),
        ("checklist", "商品上线检查清单",
         "# 商品模块上线检查清单\n## 历史问题\n价格字段曾出现精度丢失问题，需检查 decimal 精度。\n## 检查项\n- 商品创建后列表可查询\n- 价格为0时被拒绝"),
    ]
    store = create_vectorstore(db)
    embedder = create_embedder()
    # 先收集全部分片并一次性 fit（保证向量维度稳定，避免 chroma 维度冲突）
    prepared: list[tuple] = []
    all_child_texts: list[str] = []
    for doc_type, name, content in docs:
        parents = split_document_structured(content, doc_type)
        child_texts = [c["content"] for p in parents for c in p["children"]]
        prepared.append((doc_type, name, parents, child_texts))
        all_child_texts.extend(child_texts)
    if isinstance(embedder, TfidfEmbedder):
        embedder.fit([tokenize(t) for t in all_child_texts])
    for doc_type, name, parents, child_texts in prepared:
        doc = KnowledgeDoc(project_id=PROJECT_ID, doc_type=doc_type, name=name, status="processing")
        db.add(doc)
        db.flush()
        vectors = embedder.embed(child_texts)
        n = 0
        for pi, p in enumerate(parents):
            parent = KnowledgeChunk(doc_id=doc.id, seq=pi, content=p["parent_content"],
                                    vector=[], is_parent=True, source=f"{name}#父{pi + 1}", doc_type=doc_type)
            db.add(parent)
            db.flush()
            for c in p["children"]:
                child = KnowledgeChunk(doc_id=doc.id, seq=c["seq"], content=c["content"], vector=vectors[n],
                                       parent_id=parent.id, source=f"{name}#{c['seq'] + 1}", doc_type=doc_type)
                db.add(child)
                if store.name != "db":
                    db.flush()
                    store.upsert(child.id, vectors[n], {"project_id": PROJECT_ID, "doc_type": doc_type})
                n += 1
        doc.status = "ready"
        doc.chunk_count = n
        db.commit()
    embedder_holder["embedder"] = embedder
    return store


def main() -> None:
    from web.db import SessionLocal, init_db
    init_db()

    # ================= B12 知识库注入用例生成 =================
    print("\n[B12] 知识库注入用例生成（操作手册/基线用例/上线检查清单）")
    from knowledge.requirement_rag import build_case_context
    holder: dict = {}
    with SessionLocal() as db:
        seed_case_knowledge(db, holder)
        ctx = build_case_context(db, PROJECT_ID, "商品发布的价格校验与规格限制", top_k=2)
        check("build_case_context 返回非空上下文", bool(ctx))
        for sec in ("【相关模块操作手册", "【相关模块历史基线用例", "【上线检查清单"):
            check(f"包含三类知识之一: {sec}", sec in ctx)
        # 来源可追溯
        check("上下文含来源引用", "来源：" in ctx)
        # 空库降级：不存在的 project 返回空串
        ctx2 = build_case_context(db, 999999, "不存在的需求")
        check("空库静默降级为空串", ctx2 == "")

    # ================= A3 分层多趟提取 =================
    print("\n[A3] 分层多趟提取（Pass1功能面/Pass2规则面/Pass3依赖面/Pass4异常面）")
    from knowledge.requirement_parser import parse_requirement
    res = parse_requirement(REQUIREMENT, FakeLLM(), endpoints=ENDPOINTS)
    check("Pass1 功能面：条目数 = 2", len(res.items) == 2, str(len(res.items)))
    check("Pass1 功能面：acceptance 已提取", all(it.acceptance for it in res.items))
    check("Pass2 规则面：rules 合并", any("价格必须大于0" in it.rules for it in res.items))
    check("Pass2 规则面：constraints 合并", any("用户名长度≤200" in it.constraints for it in res.items))
    check("Pass3 依赖面：involved_endpoints 合并", any(("POST", "/api/v1/login") in [(e["method"], e["path"]) for e in it.involved_endpoints] for it in res.items))
    check("Pass3 依赖面：upstream_systems", any("用户中心" in it.upstream_systems for it in res.items))
    check("Pass3 依赖面：downstream_systems", any("凭证服务" in it.downstream_systems for it in res.items))
    check("Pass3 依赖面：data_compat_rules", any("兼容旧数据" in " ".join(it.data_compat_rules) for it in res.items))
    check("Pass4 异常面：risks", any("重试策略" in r for r in res.risks))
    check("Pass4 异常面：edge_cases", any("超长" in e for e in res.edge_cases))
    check("Pass4 异常面：anomalies", any("错误码" in a for a in res.anomalies))
    check("pass_report 记录 4 趟", set(res.pass_report) == {"Pass1功能面", "Pass2规则面", "Pass3依赖面", "Pass4异常面"}, str(res.pass_report))
    # 接口引用只保留真实端点
    bad = [it.id for it in res.items for e in it.involved_endpoints
           if (e["method"], e["path"]) not in [(x["method"], x["path"]) for x in ENDPOINTS]]
    check("接口引用过滤（无虚构端点）", not bad, str(bad))

    # ================= A12 动态代码模式检查 =================
    print("\n[A12] 动态代码模式检查（Job/MQ/延迟消息）")
    from adapters.base import ChangedFunction
    from whitebox.pattern_check import check_patterns, detect_patterns

    job_fn = ChangedFunction("scheduler.py", "daily_settle_job", 1, 10, [1],
                             source_code="def daily_settle_job():\n    while True:\n        run_settle()\n        time.sleep(60)",
                             signature="def daily_settle_job():")
    mq_fn = ChangedFunction("consumer.py", "on_order_message", 1, 10, [1],
                            source_code="@RabbitListener(queue='order')\ndef on_order_message(msg):\n    process(msg)\n    ack(msg)",
                            signature="def on_order_message(msg):")
    delay_fn = ChangedFunction("delay.py", "handle_delay_retry", 1, 10, [1],
                               source_code="def handle_delay_retry():\n    ttl = config.TTL\n    if expired(msg):\n        cancel(order_id)",
                               signature="def handle_delay_retry():")
    plain_fn = ChangedFunction("util.py", "format_price", 1, 5, [1],
                               source_code="def format_price(p):\n    return round(p, 2)", signature="def format_price(p):")
    hits = detect_patterns([job_fn, mq_fn, delay_fn, plain_fn])
    hit_keys = {h["pattern_key"] for h in hits}
    check("识别 定时任务/Job 模式", "scheduled_job" in hit_keys, str(hit_keys))
    check("识别 MQ 消费模式", "mq_consumer" in hit_keys)
    check("识别 延迟消息模式", "delay_message" in hit_keys)
    check("普通函数不误报", not any(h["function"] == "format_price" for h in hits))
    findings = check_patterns([job_fn, mq_fn])
    check("命中模式生成专属检查清单（含汇总项）", any("checklist" in f and len(f["checklist"]) >= 3 for f in findings))
    check("严重/高风险检查项单独成条", any(f["severity"] == "严重" and "死循环" in f["issue"] for f in findings))
    check("MQ 幂等检查项存在", any("幂等" in f["issue"] for f in findings))

    # ================= A10 契约变化检测 =================
    print("\n[A10] 契约变化检测（签名/枚举/路由）")
    from whitebox.contract_check import detect_contract_changes
    repo = Path(_TMPDIR) / "contract_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    (repo / "svc.py").write_text(
        "from enum import Enum\nclass Status(Enum):\n    OK = 'ok'\n    PENDING = 'pending'\n\ndef greet(name, age):\n    return f'{name}:{age}'\n"
        "@app.get('/api/v1/items')\ndef items():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    base = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    (repo / "svc.py").write_text(
        "from enum import Enum\nclass Status(Enum):\n    OK = 'ok'\n    PENDING = 'pending'\n    CANCELED = 'canceled'\n\ndef greet(name):\n    return name\n"
        "@app.post('/api/v1/items')\ndef items():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "target"], check=True)
    target = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()

    from whitebox.git_diff import get_changed_functions
    funcs = get_changed_functions(str(repo), base, target)
    contract = detect_contract_changes(str(repo), base, target, funcs)
    types = {c["type"] for c in contract}
    check("公共方法签名变更被检测（greet 删参数）", "公共方法签名变更" in types, str(types))
    sig = next((c for c in contract if c["type"] == "公共方法签名变更"), None)
    if sig:
        check("签名变更 severity=高（删参数）", sig["severity"] == "高")
        check("签名变更证据含新旧签名", any("旧签名" in e for e in sig["evidence"]))
    check("新增枚举成员被检测（CANCELED，文件含枚举）", "新增枚举/枚举成员" in types)
    check("接口路由变化被检测（GET→POST）", "接口路径/方法/参数变化" in types)

    # ================= A17 结构化映射 + 双向追溯 =================
    print("\n[A17] 结构化映射 + 双向追溯")
    from core.models import FunctionalCase
    case = FunctionalCase(id="FC-001", feature="用户登录", title="正确凭证登录", category="normal",
                          preconditions="已注册", steps=["构造用户名密码", "调用登录接口", "校验token"],
                          test_data={"username": "admin", "password": "123456"}, expected="返回200含token",
                          involved_endpoints=[{"method": "POST", "path": "/api/v1/login"}])
    check("step_refs 生成 FC-001.1/2/3", case.step_refs() == ["FC-001.1", "FC-001.2", "FC-001.3"], str(case.step_refs()))
    check("numbered_steps 序号化", case.numbered_steps()[0].startswith("FC-001.1 构造"), case.numbered_steps()[0])
    ss = case.structured_steps()
    check("structured_steps [序号,动作,目标,数据]", ss[0]["seq"] == "FC-001.1" and "POST /api/v1/login" in ss[0]["target"]
          and ss[0]["data"] == case.test_data)
    # UI 脚本：// Step 注释强制 + step_map
    from agents.ui_tester import UITester
    script, step_map = UITester._ensure_step_comments(
        "await agent.aiNavigate('http://x');\nawait agent.aiInput('用户名', 'admin');\nawait agent.aiTap('登录');")
    check("UI 脚本自动补 // Step 注释", script.count("// Step") == 3, script.splitlines()[1][:40])
    check("step_map 与脚本行对应", len(step_map) == 3 and step_map[0]["step"] == "1.1" and step_map[0]["code"].startswith("await agent.aiNavigate"))
    # 已有注释保留（LLM 已标注）
    script2, step_map2 = UITester._ensure_step_comments(
        "// Step 2.1: 输入用户名\nawait agent.aiInput('用户名', 'admin');")
    check("LLM 已有 // Step 注释被保留", step_map2[0]["step"] == "2.1" and script2.count("// Step") == 1)
    # API pytest：Step 注释回链
    from agents.api_tester import APITester
    code = APITester._render_pytest([
        {"name": "login_normal", "method": "POST", "path": "/api/v1/login", "params": {},
         "expect_status": 200, "expect_fields": ["token"]},
    ], "http://127.0.0.1:8100")
    check("pytest 脚本含 # Step 1.1 注释", "# Step 1.1:" in code and "login_normal" in code)
    # 功能用例 markdown 序号化
    from agents.functional_tester import FunctionalTester
    payload = {"requirement": "x", "features": ["用户登录"],
               "cases": [case.to_dict()], "generator_model": "mock"}
    md = FunctionalTester._render_markdown(payload)
    check("功能用例 md 步骤序号化", "FC-001.1 构造用户名密码" in md and "FC-001.2 调用登录接口" in md)

    print("\n" + ("=== ALL P1 GAPS VERIFY PASSED ===" if not FAILED else f"=== {len(FAILED)} FAILED: {FAILED} ==="))
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
