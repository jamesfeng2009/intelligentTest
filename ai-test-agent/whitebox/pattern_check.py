"""动态代码模式检查（A12）—— 模式感知的精准加固。

它不是通用代码检查，而是在识别到 Job/定时任务、MQ 消费、延迟消息等特定代码形态时，
自动激活专属检查清单，覆盖通用走查容易遗漏的"场景化隐性风险"：
- 定时任务空跑死循环 / 多实例重复执行 / 异常静默中断
- MQ 重复消费导致数据错乱 / ack 语义不清导致消息丢失 / 坏消息阻塞队列
- 延迟消息 TTL 不匹配引发兜底失效 / 过期消息无处理路径

识别方式：函数名 + 源码标记双重匹配（语言无关），命中即逐项输出专属检查意见。
"""
from __future__ import annotations

import re
from typing import Any

# 动态模式定义：name_markers 命中函数名即激活；source_markers 命中 >= 2 处也激活
PATTERN_DEFS: dict[str, dict[str, Any]] = {
    "scheduled_job": {
        "name": "定时任务/Job",
        "desc": "定时/周期执行的任务（cron / @Scheduled / xxxJob / ticker / 定时扫描）",
        "name_markers": ["job", "cron", "schedule", "scheduled", "task", "tick", "timer", "periodic", "定时", "扫表"],
        "source_markers": [
            r"\bcron\b", r"\bschedule[sd]?\b", r"@Scheduled", r"\bJob\b", r"\bticker\b", r"\btimer\b",
            r"\bevery\s*\(", r"\binterval\b", r"\bperiodic\b", r"time\.Sleep", r"time\.NewTicker",
        ],
        "checks": [
            {"key": "empty_loop", "severity": "严重",
             "issue": "定时任务存在空跑/死循环风险：循环/重试缺少退出条件或退避",
             "suggestion": "为循环/重试设置最大次数与指数退避，空跑时记录日志并告警"},
            {"key": "idempotency", "severity": "高",
             "issue": "定时任务未确认幂等：重试/补跑可能导致同一批数据被重复处理",
             "suggestion": "为任务增加幂等键/处理状态位，重复执行应安全跳过"},
            {"key": "distributed_lock", "severity": "高",
             "issue": "多实例部署下定时任务可能被多个节点重复执行（重复发券/重复结算等）",
             "suggestion": "使用分布式锁（Redis 锁 / DB 行锁 / ShedLock）保证单实例执行"},
            {"key": "exception_swallow", "severity": "警告",
             "issue": "异常被吞掉（except: pass 等）时任务失败无感知，可能静默中断",
             "suggestion": "记录日志并告警，必要时引入失败重试/补偿"},
            {"key": "retry_bounded", "severity": "警告",
             "issue": "重试无上限（while True/无限循环）可能造成任务卡死或堆积",
             "suggestion": "重试设置最大次数与死信/告警出口"},
        ],
    },
    "mq_consumer": {
        "name": "MQ 消费",
        "desc": "消息队列消费/订阅（consumer / onMessage / @RabbitListener / @KafkaListener / 订阅处理）",
        "name_markers": ["consumer", "consume", "onmessage", "listener", "subscribe", "handler", "processor", "消费", "订阅"],
        "source_markers": [
            r"\bconsumer\b", r"\bconsume\b", r"onMessage", r"@RabbitListener", r"@KafkaListener",
            r"\bsubscribe\b", r"\bpoll\b", r"\bqueue\b", r"\btopic\b", r"\back\b", r"\bnack\b",
        ],
        "checks": [
            {"key": "idempotency", "severity": "严重",
             "issue": "MQ 重复消费未确认幂等：重投/重试可能导致数据重复入库或重复处理",
             "suggestion": "消费逻辑必须幂等（唯一业务键/去重表），重复消息应被识别并跳过"},
            {"key": "ack_nack", "severity": "高",
             "issue": "未看到明确 ack/nack/commit 处理：异常时消息可能丢失或被无限重投",
             "suggestion": "明确 ack 语义：处理成功确认，失败按策略 nack/重试/进死信"},
            {"key": "exception_swallow", "severity": "警告",
             "issue": "消费异常被吞掉可能导致消息丢失且无告警",
             "suggestion": "异常必须记录日志并触发告警/死信，禁止静默 pass"},
            {"key": "dead_letter", "severity": "警告",
             "issue": "消费失败缺少死信/兜底出口，坏消息可能阻塞队列或无限重投",
             "suggestion": "失败超过阈值进入死信队列/延迟重试队列，并监控积压"},
            {"key": "ordering", "severity": "建议",
             "issue": "若业务依赖消息顺序，需确认分区/单线程消费策略",
             "suggestion": "按业务键路由到固定分区，或串行消费保证顺序"},
        ],
    },
    "delay_message": {
        "name": "延迟消息/超时兜底",
        "desc": "延迟消息、TTL/过期、重试、超时关闭/取消等兜底逻辑",
        "name_markers": ["delay", "ttl", "expire", "expired", "retry", "timeout", "cancel", "close", "dead", "延迟", "超时", "过期", "兜底"],
        "source_markers": [
            r"\bdelay\b", r"\bttl\b", r"\bexpire", r"\bexpired\b", r"\bretry\b", r"\btimeout\b",
            r"dead.?letter", r"redeliver", r"cancel", r"\bclose\b",
        ],
        "checks": [
            {"key": "ttl_match", "severity": "高",
             "issue": "延迟消息 TTL/过期时间与业务预期不匹配，可能提前失效或永久滞留",
             "suggestion": "核对 TTL 与业务超时时间一致，过期消息要有明确处理路径"},
            {"key": "fallback_valid", "severity": "高",
             "issue": "延迟消息的兜底/补偿逻辑缺失：消息丢失时业务（如订单超时关闭）无人处理",
             "suggestion": "必须有定时兜底扫描（cancel/close/expire）作为延迟消息失效后的补偿"},
            {"key": "expired_handling", "severity": "警告",
             "issue": "过期消息处理路径不明确（丢弃/告警/补偿）",
             "suggestion": "明确过期消息策略并记录日志，避免静默丢弃"},
            {"key": "accumulation", "severity": "警告",
             "issue": "延迟队列积压可能放大超时/兜底压力",
             "suggestion": "监控延迟队列深度与兜底扫描性能，积压时告警"},
        ],
    },
}


def detect_patterns(funcs: list[Any]) -> list[dict]:
    """识别变更函数命中的动态模式。

    返回 [{function, file, pattern_key, pattern_name, desc, matched_markers, check_count}]。
    命中规则：函数名含 name_marker 即命中；否则源码命中 source_markers >= 2 处也命中。
    """
    hits: list[dict] = []
    for f in funcs:
        name = str(getattr(f, "function_name", "")).lower()
        source = (getattr(f, "source_code", "") or getattr(f, "full_source", "") or "").lower()
        for key, pat in PATTERN_DEFS.items():
            name_hit = any(m.lower() in name for m in pat["name_markers"])
            src_hits = [m for m in pat["source_markers"] if re.search(m, source)]
            if name_hit or len(src_hits) >= 2:
                hits.append({
                    "function": getattr(f, "function_name", "?"),
                    "file": getattr(f, "file_path", "?"),
                    "pattern_key": key,
                    "pattern_name": pat["name"],
                    "desc": pat["desc"],
                    "matched_markers": (["函数名:" + m for m in pat["name_markers"] if m.lower() in name]
                                        + [f"源码:{m}" for m in src_hits])[:8],
                    "check_count": len(pat["checks"]),
                })
    return hits


def check_patterns(funcs: list[Any]) -> list[dict]:
    """对命中动态模式的变更函数生成专属检查意见（A12）。

    返回 findings：每个命中模式生成一条汇总意见（含完整专属检查清单），
    严重/高风险检查项同时单独输出，便于在走查报告中按严重度排序。
    """
    findings: list[dict] = []
    for hit in detect_patterns(funcs):
        pat = PATTERN_DEFS[hit["pattern_key"]]
        location = f"{hit['file']}:{hit['function']}"
        # 汇总意见（带完整检查清单）
        checklist = [{"key": c["key"], "severity": c["severity"], "issue": c["issue"], "suggestion": c["suggestion"]}
                     for c in pat["checks"]]
        high = [c for c in pat["checks"] if c["severity"] in ("严重", "高")]
        max_sev = "严重" if any(c["severity"] == "严重" for c in pat["checks"]) else (
            "高" if high else "警告")
        findings.append({
            "source": "动态模式检查(A12)",
            "severity": max_sev,
            "location": location,
            "issue": f"命中动态模式「{hit['pattern_name']}」：{hit['desc']}，激活专属检查清单（{len(checklist)} 项）",
            "suggestion": "按专属检查清单逐项核验；重点：{0}".format("、".join(c["key"] for c in high)),
            "pattern": hit["pattern_key"],
            "pattern_name": hit["pattern_name"],
            "matched_markers": hit["matched_markers"],
            "checklist": checklist,
        })
        # 严重/高风险检查项单独成条（便于报告按严重度展示）
        for c in high:
            findings.append({
                "source": "动态模式检查(A12)",
                "severity": c["severity"],
                "location": location,
                "issue": f"[{hit['pattern_name']}] {c['issue']}",
                "suggestion": c["suggestion"],
                "pattern": hit["pattern_key"],
                "checklist_key": c["key"],
            })
    return findings
