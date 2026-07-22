"""実行記録を決定的な情報源とする処理報告の生成。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from story_pipeline.persistence import atomic_write_text


@dataclass(frozen=True, slots=True)
class FileChange:
    path: str
    change: str


@dataclass(frozen=True, slots=True)
class HumanDecision:
    identifier: str
    question: str
    impact: str
    choices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReportContext:
    request_summary: str
    kind: str
    targets: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    applied_priority: tuple[str, ...] = ()
    changed_files: tuple[FileChange, ...] = ()
    adoption_reason: str | None = None
    cautions: tuple[str, ...] = ()
    story_changes: tuple[str, ...] = ()
    unresolved: tuple[str, ...] = ()
    decisions: tuple[HumanDecision, ...] = ()
    next_action: str = "なし"


def render_run_report(run: dict[str, Any], context: ReportContext) -> str:
    """run と採用情報から必須9節を持つ Markdown を生成する。"""
    number = run["request_number"]
    lines = [
        f"# 要求 {number:04d} 処理報告",
        "",
        f"Status: `{_inline(run['status'])}`",
        "",
        "## 受け付けた要求",
        "",
        f"- 要求番号: `{number:04d}`",
        f"- 要約: {_inline(context.request_summary)}",
        "",
        "## 要求の解釈",
        "",
        f"- kind: `{_inline(context.kind)}`",
        *_bullet_group("対象", context.targets),
        *_bullet_group("仮定", context.assumptions),
        *_bullet_group("適用した優先順位", context.applied_priority),
        "",
        "## 実行した処理",
        "",
        *_step_lines(run["steps"]),
        "",
        "## 作成・変更したファイル",
        "",
        *_file_lines(context.changed_files),
        "",
        "## 評価と改稿",
        "",
        *_evaluation_lines(run, context),
        "",
        "## 物語・設定上の変更",
        "",
        *_plain_bullets(context.story_changes),
        "",
        "## 未解決の問題",
        "",
        *_unresolved_lines(run, context.unresolved),
        "",
        "## 人間に判断してほしい事項",
        "",
        *_decision_lines(context.decisions),
        "",
        "## 次回の標準処理",
        "",
        _inline(context.next_action),
        "",
    ]
    return "\n".join(lines)


def write_run_report(root: Path, run: dict[str, Any], context: ReportContext) -> str:
    """要求番号に対応する agent 報告を原子的に保存し、相対パスを返す。"""
    relative = f"requests/{run['request_number']:04d}_agent.md"
    atomic_write_text(root / relative, render_run_report(run, context))
    return relative


def _step_lines(steps: list[dict[str, Any]]) -> list[str]:
    if not steps:
        return ["なし"]
    return [
        f"- `{_inline(step['id'])}`: `{_inline(step['status'])}`"
        + (f" — {_inline(step['result'])}" if step.get("result") else "")
        for step in steps
    ]


def _file_lines(changes: tuple[FileChange, ...]) -> list[str]:
    if not changes:
        return ["なし"]
    return [f"- `{_inline(item.path)}`: {_inline(item.change)}" for item in changes]


def _evaluation_lines(run: dict[str, Any], context: ReportContext) -> list[str]:
    counts = run["call_counts"]
    lines = [
        "- 呼び出し数: "
        + ", ".join(f"{name}={counts[name]}" for name in ("generation", "review", "revision", "summary"))
    ]
    models = sorted(
        {
            (str(item.get("role", "unknown")), str(item.get("api_model", "unknown")))
            for item in run["model_attempts"]
        }
    )
    lines.append(
        "- 使用モデル: "
        + (", ".join(f"{_inline(role)}={_inline(model)}" for role, model in models) if models else "なし")
    )
    metrics = run.get("metrics")
    if isinstance(metrics, dict):
        lines.extend(_performance_lines(run, metrics))
    lines.append(f"- 採用理由: {_inline(context.adoption_reason or 'なし')}")
    lines.extend(_bullet_group("残る注意", context.cautions))
    return lines


def _performance_lines(run: dict[str, Any], metrics: dict[str, Any]) -> list[str]:
    usage = metrics["usage"]
    usage_text = ", ".join(
        f"{name}={usage[name] if usage[name] is not None else 'unknown'}"
        for name in (
            "prompt_tokens", "completion_tokens", "total_tokens",
            "cached_tokens", "reasoning_tokens",
            "cost_usd",
        )
    )
    fallbacks = sum(item.get("fallback_count", 0) for item in run.get("model_calls", ()))
    truncations = sum(bool(item.get("truncated")) for item in run.get("model_calls", ()))
    lines = [
        "- 通信計測: "
        f"logical={metrics['logical_calls']}, transport={metrics['transport_attempts']}, "
        f"retry_wait_ms={metrics['retry_wait_ms']}, elapsed_ms={metrics['elapsed_ms']}, "
        f"fallbacks={fallbacks}, truncations={truncations}",
        f"- Token usage: {usage_text}",
    ]
    if run.get("resume_count", 0) > 0:
        regenerated = sum(
            item.get("category") == "knowledge" and item.get("resume_count", 0) > 0
            for item in run.get("model_calls", ())
        )
        lines.append(f"- 再開後 knowledge 再生成: {regenerated}")
    return lines


def _unresolved_lines(run: dict[str, Any], unresolved: tuple[str, ...]) -> list[str]:
    lines = [f"- {_inline(item)}" for item in unresolved]
    lines.extend(
        f"- `{_inline(error['step'])}` / `{_inline(error['category'])}`: {_inline(error['message'])}"
        for error in run["errors"]
    )
    if run.get("resume") is not None:
        resume = run["resume"]
        lines.append(f"- 再開位置 `{_inline(resume['step'])}`: {_inline(resume['reason'])}")
    return lines or ["なし"]


def _decision_lines(decisions: tuple[HumanDecision, ...]) -> list[str]:
    if not decisions:
        return ["なし"]
    lines: list[str] = []
    for item in decisions:
        choices = " / ".join(_inline(choice) for choice in item.choices)
        lines.append(
            f"- `{_inline(item.identifier)}`: {_inline(item.question)}"
            f"（影響: {_inline(item.impact)}、選択肢: {choices}）"
        )
    return lines


def _bullet_group(label: str, values: tuple[str, ...]) -> list[str]:
    return [f"- {label}: " + (" / ".join(_inline(value) for value in values) if values else "なし")]


def _plain_bullets(values: tuple[str, ...]) -> list[str]:
    return [f"- {_inline(value)}" for value in values] or ["なし"]


def _inline(value: object) -> str:
    return " ".join(str(value).replace("\x00", "").splitlines()).strip() or "なし"
