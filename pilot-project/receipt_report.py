"""Describe receipt claims without promoting fixtures into App evidence."""


def describe_receipt(receipt: dict) -> dict:
    """Keep execution, learning, usage and skill evidence independent."""
    provenance = receipt.get("input_provenance")
    if provenance == "real_codex_app_hooks":
        execution_evidence = "app"
    elif isinstance(provenance, str) and provenance.startswith("synthetic"):
        execution_evidence = "synthetic"
    else:
        execution_evidence = "unknown"

    hook_events = receipt.get("hook_events")
    app_capture_verified = bool(
        execution_evidence == "app"
        and all(
            isinstance(receipt.get(field), str) and receipt[field].strip()
            for field in ("session_id", "turn_id")
        )
        and isinstance(hook_events, (list, tuple))
        and all(
            event in hook_events
            for event in ("UserPromptSubmit", "PostToolUse", "Stop")
        )
    )

    skill_loaded = receipt.get("skill_loaded") is True
    skill_adopted = receipt.get("skill_adopted") is True
    skill_stage = "adopted" if skill_adopted else "loaded" if skill_loaded else None
    skill_validated = (
        skill_loaded
        and skill_adopted
        and receipt.get("skill_validation_passed") is True
    )

    return {
        "execution_evidence": execution_evidence,
        "app_capture_verified": app_capture_verified,
        "learning_status": receipt.get("run_status"),
        "applied_operations": receipt.get("applied_operations"),
        "api_calls": receipt.get("api_calls"),
        "actual_fee_usd": receipt.get("actual_fee_usd"),
        "skill_stage": skill_stage,
        "skill_validated": skill_validated,
    }


def render_markdown(receipt: dict) -> str:
    """Render receipt claims in Chinese without adding evidence or fees."""
    report = describe_receipt(receipt)

    evidence = "未知"
    if report["execution_evidence"] == "synthetic":
        evidence = "合成样例（不能证明 App 采集）"
    elif report["execution_evidence"] == "app":
        evidence = (
            "Codex App（采集已核验）"
            if report["app_capture_verified"]
            else "Codex App 来源（采集未核验）"
        )

    status = {
        "queued": "排队中",
        "running": "进行中",
        "completed": "已完成",
        "failed": "失败",
        "interrupted": "已中断",
        "reconciling": "核对中",
        "deferred": "已延后",
        "superseded": "已被取代",
    }.get(report["learning_status"], "未知")
    skill = {"loaded": "已加载", "adopted": "已采用"}.get(
        report["skill_stage"], "未知"
    )
    if report["skill_validated"]:
        skill += "（已验证）"

    def value_with_unit(value, unit: str) -> str:
        return "未知" if value is None else f"{value} {unit}"

    return "\n".join(
        (
            f"- 采集证据：{evidence}",
            f"- 复盘状态：{status}",
            f"- 已生效变更：{value_with_unit(report['applied_operations'], '项')}",
            f"- 技能阶段：{skill}",
            f"- API 调用次数：{value_with_unit(report['api_calls'], '次')}",
            f"- 实际费用：{value_with_unit(report['actual_fee_usd'], 'USD')}",
        )
    )
