"""评估数据集（T44）：≥50 条标注用例质量样本，格式可复算。

- 三类样本：api_case / ui_script / whitebox_case
- 每样本带人工标注 labels：coverage（覆盖度）/ correctness（正确性）/ stability（稳定性） 1-5 分
- 标注为确定性生成（模板 + 组合），可复算、可被 Grader 对照
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from web.models import EvalSample

# (category, title, prompt, test_case, labels) —— 20 条 api + 16 条 ui + 16 条 whitebox = 52 条
_RAW: list[tuple[str, str, str, str, dict]] = [
    # ---- api_case 20 条 ----
    ("api_case", "商品创建-正常", "创建商品，参数完整合法", "POST /api/v1/products {name:测试商品,price:99.9} → 201,id+name+price", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("api_case", "商品创建-缺字段", "创建商品缺少 name", "POST /api/v1/products {} → 422，明确字段错误", {"coverage": 5, "correctness": 4, "stability": 5}),
    ("api_case", "商品创建-超长名", "name 超过 200 字符", "POST /api/v1/products {name:x*201} → 422 或截断", {"coverage": 4, "correctness": 4, "stability": 4}),
    ("api_case", "商品创建-负数价格", "price 为负数", "POST /api/v1/products {price:-1} → 422", {"coverage": 5, "correctness": 5, "stability": 4}),
    ("api_case", "商品创建-字符串价格", "price 传入字符串", "POST /api/v1/products {price:'abc'} → 422", {"coverage": 4, "correctness": 4, "stability": 5}),
    ("api_case", "商品创建-超大价格", "price 超过上限", "POST /api/v1/products {price:1e9} → 422 或拒绝", {"coverage": 4, "correctness": 3, "stability": 4}),
    ("api_case", "商品列表-正常", "查询全部商品", "GET /api/v1/products → 200，数组结构", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("api_case", "商品列表-空库", "无商品时查询", "GET /api/v1/products → 200 []", {"coverage": 4, "correctness": 4, "stability": 5}),
    ("api_case", "商品详情-存在", "按 id 查询已有商品", "GET /api/v1/products/1001 → 200 商品对象", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("api_case", "商品详情-不存在", "按不存在的 id 查询", "GET /api/v1/products/999999 → 404", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("api_case", "商品详情-非法id", "id 为非法类型", "GET /api/v1/products/abc → 422", {"coverage": 4, "correctness": 4, "stability": 5}),
    ("api_case", "登录-正常", "正确账号密码", "POST /api/v1/auth/login {admin,123456} → 200 token+user", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("api_case", "登录-错误密码", "密码错误", "POST /api/v1/auth/login {admin,xxx} → 401", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("api_case", "登录-不存在用户", "用户不存在", "POST /api/v1/auth/login {ghost,123} → 401", {"coverage": 4, "correctness": 5, "stability": 5}),
    ("api_case", "登录-超长用户名", "用户名超长", "POST /api/v1/auth/login {x*200,123456} → 401/422", {"coverage": 4, "correctness": 3, "stability": 4}),
    ("api_case", "接口-未知路由", "访问不存在接口", "GET /api/v1/nothing → 404", {"coverage": 4, "correctness": 5, "stability": 5}),
    ("api_case", "接口-方法不允许", "用 GET 调 POST 接口", "GET /api/v1/products（POST 语义）→ 405", {"coverage": 4, "correctness": 4, "stability": 5}),
    ("api_case", "并发-重复创建", "并发创建相同商品", "并发 POST 同一商品 → 幂等或各自创建不崩溃", {"coverage": 4, "correctness": 3, "stability": 3}),
    ("api_case", "响应-字段类型", "校验返回字段类型", "全部接口返回 JSON 字段类型与契约一致", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("api_case", "响应-耗时", "接口响应耗时基线", "核心接口 P95 < 500ms", {"coverage": 3, "correctness": 4, "stability": 3}),
    # ---- ui_script 16 条 ----
    ("ui_script", "登录-UI正常", "UI 登录主流程", "打开首页→输入 admin/123456→点登录→断言欢迎语出现", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("ui_script", "登录-错误密码UI", "UI 登录错误密码", "输入错误密码→点登录→断言错误提示", {"coverage": 4, "correctness": 5, "stability": 4}),
    ("ui_script", "登录-空账号UI", "UI 空账号提交", "不输入账号直接提交→断言校验提示", {"coverage": 4, "correctness": 4, "stability": 5}),
    ("ui_script", "新增商品-UI正常", "UI 新增商品主流程", "登录后→填名称/价格→点新增→断言创建成功提示与列表项", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("ui_script", "新增商品-空价格", "UI 价格为空提交", "填名称不填价格→提交→断言校验提示", {"coverage": 4, "correctness": 4, "stability": 5}),
    ("ui_script", "新增商品-超长名称", "UI 超长名称提交", "名称超长→提交→断言被拒绝或截断展示", {"coverage": 4, "correctness": 3, "stability": 4}),
    ("ui_script", "列表-数据展示", "UI 商品列表渲染", "登录后断言列表展示已有商品与价格", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("ui_script", "列表-空态", "UI 空列表展示", "无数据时断言空态文案存在", {"coverage": 4, "correctness": 4, "stability": 4}),
    ("ui_script", "导航-页面跳转", "UI 页面导航", "点击导航项→断言目标页面标题", {"coverage": 4, "correctness": 5, "stability": 5}),
    ("ui_script", "表单-重置", "UI 表单提交后重置", "新增成功后断言表单清空", {"coverage": 4, "correctness": 4, "stability": 4}),
    ("ui_script", "登录态-保持", "UI 刷新保持登录态", "登录后刷新页面→断言仍为登录态（或明确的无状态设计）", {"coverage": 4, "correctness": 3, "stability": 3}),
    ("ui_script", "移动端-宽度适配", "UI 窄屏布局", "375px 宽下断言无横向溢出", {"coverage": 3, "correctness": 3, "stability": 3}),
    ("ui_script", "按钮-重复点击", "UI 按钮防抖", "快速双击新增→断言只创建一次", {"coverage": 4, "correctness": 3, "stability": 3}),
    ("ui_script", "文案-商品价格格式", "UI 价格展示格式", "断言价格以 ¥ + 两位小数展示", {"coverage": 4, "correctness": 5, "stability": 5}),
    ("ui_script", "登录-退出", "UI 退出登录", "登录后退出→断言回到登录页", {"coverage": 4, "correctness": 5, "stability": 4}),
    ("ui_script", "首页-加载", "UI 首屏加载", "打开首页→断言核心模块在超时内出现", {"coverage": 3, "correctness": 4, "stability": 3}),
    # ---- whitebox_case 16 条 ----
    ("whitebox_case", "登录-密码校验", "密码校验逻辑单测", "verify_password 正确/错误/空密码三路断言", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("whitebox_case", "登录-用户查找", "用户查找分支单测", "find_user 存在/不存在/异常输入", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("whitebox_case", "登录-超时锁定", "多次失败锁定逻辑", "failed_attempts 超过阈值→锁定；未超→不锁定", {"coverage": 4, "correctness": 4, "stability": 4}),
    ("whitebox_case", "商品-价格计算", "价格计算边界", "正常价/0/负数/极大值四路", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("whitebox_case", "商品-名称校验", "名称长度校验", "边界长度 0/1/200/201 字符", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("whitebox_case", "商品-库存扣减", "库存不足处理", "扣减>库存→拒绝；等于→成功；并发扣减", {"coverage": 4, "correctness": 3, "stability": 3}),
    ("whitebox_case", "工具-时间格式", "时间格式化", "合法/非法/None 输入", {"coverage": 4, "correctness": 5, "stability": 5}),
    ("whitebox_case", "工具-金额取整", "金额舍入逻辑", "四舍五入/银行家舍入/负数", {"coverage": 4, "correctness": 4, "stability": 4}),
    ("whitebox_case", "鉴权-token解析", "token 解析与过期", "合法/过期/伪造 token 三路", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("whitebox_case", "鉴权-权限判定", "角色权限分支", "admin/qa/developer/viewer 权限矩阵", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("whitebox_case", "缓存-命中失效", "缓存读写", "命中/未命中/失效后重建", {"coverage": 4, "correctness": 4, "stability": 4}),
    ("whitebox_case", "缓存-并发写", "缓存并发写一致性", "多线程写同一 key 不产生脏数据", {"coverage": 3, "correctness": 3, "stability": 3}),
    ("whitebox_case", "校验-空指针防护", "空输入防护", "None/空串/空列表不崩溃", {"coverage": 5, "correctness": 5, "stability": 5}),
    ("whitebox_case", "校验-类型错误", "类型不匹配处理", "错误类型抛 TypeError 而非静默", {"coverage": 4, "correctness": 4, "stability": 5}),
    ("whitebox_case", "重试-指数退避", "重试策略", "失败重试次数上限与退避间隔", {"coverage": 3, "correctness": 4, "stability": 3}),
    ("whitebox_case", "日志-脱敏", "日志敏感信息脱敏", "密码/token 不出现在日志", {"coverage": 4, "correctness": 5, "stability": 5}),
]


def seed_datasets(db: Session, reset: bool = False) -> int:
    """把标注样本写入 eval_samples 表（幂等：存在同 category+title 则跳过）。"""
    if reset:
        db.query(EvalSample).delete()
        db.flush()
    count = 0
    for category, title, prompt, test_case, labels in _RAW:
        exists = db.query(EvalSample).filter_by(category=category, title=title).first()
        if exists:
            continue
        db.add(EvalSample(category=category, title=title, prompt=prompt,
                          test_case=test_case, labels=labels, status="pending"))
        count += 1
    db.commit()
    return count


def dataset_stats(db: Session) -> dict:
    total = db.query(EvalSample).count()
    graded = db.query(EvalSample).filter(EvalSample.status.in_(["graded", "reviewed"])).count()
    by_category = {}
    for row in db.query(EvalSample.category).all():
        by_category[row[0]] = by_category.get(row[0], 0) + 1
    return {"total": total, "graded": graded, "by_category": by_category}
