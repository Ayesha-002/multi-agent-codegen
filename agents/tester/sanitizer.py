import re
from typing import Optional

MAIN_GUARD_RE = re.compile(r'^\s*if\s+__name__\s*==\s*["\']__main__["\']\s*:\s*$')
TOP_LEVEL_DEF_RE = re.compile(r"^def\s+([A-Za-z_]\w*)\s*\(")
TOP_LEVEL_CLASS_RE = re.compile(r"^class\s+([A-Za-z_]\w*)\b")


def _strip_markdown_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[\w+-]*\n", "", cleaned)
        cleaned = re.sub(r"\n```$", "", cleaned)
    return cleaned.strip()


def _find_repeated_block_start(lines: list[str], min_block: int = 4, min_match: int = 8) -> Optional[int]:
    if len(lines) < min_match * 2:
        return None
    max_start = len(lines) - min_block
    for start in range(max_start):
        marker = lines[start:start + min_block]
        for idx in range(start + min_block, max_start + 1):
            if lines[idx:idx + min_block] != marker:
                continue
            matched = 0
            while start + matched < len(lines) and idx + matched < len(lines):
                if lines[start + matched] != lines[idx + matched]:
                    break
                matched += 1
            if matched >= min_match:
                return idx
    return None


def _dedupe_consecutive_lines(lines: list[str], max_run: int = 1) -> list[str]:
    if not lines:
        return lines
    result: list[str] = []
    prev = None
    run = 0
    for line in lines:
        if line == prev:
            run += 1
        else:
            prev = line
            run = 1
        if run <= max_run:
            result.append(line)
    return result


def _keep_single_python_main_guard(lines: list[str]) -> list[str]:
    guards = [idx for idx, line in enumerate(lines) if MAIN_GUARD_RE.match(line)]
    if len(guards) <= 1:
        return lines

    output = list(lines)
    for guard_idx in reversed(guards[1:]):
        end = guard_idx + 1
        while end < len(output):
            line = output[end]
            if MAIN_GUARD_RE.match(line):
                break
            if line.strip() == "":
                end += 1
                continue
            if line.startswith((" ", "\t")):
                end += 1
                continue
            break
        del output[guard_idx:end]
    return output


def _trim_python_restart_tail(lines: list[str]) -> list[str]:
    seen_defs: set[str] = set()
    seen_classes: set[str] = set()
    main_seen = False
    duplicate_from: Optional[int] = None

    for idx, raw_line in enumerate(lines):
        line = raw_line.rstrip()
        if line.startswith((" ", "\t")):
            continue
        stripped = line.strip()
        if not stripped:
            continue

        if MAIN_GUARD_RE.match(stripped):
            if main_seen:
                duplicate_from = idx
                break
            main_seen = True
            continue

        m_def = TOP_LEVEL_DEF_RE.match(stripped)
        if m_def:
            name = m_def.group(1)
            if name in seen_defs:
                duplicate_from = idx
                break
            seen_defs.add(name)
            continue

        m_class = TOP_LEVEL_CLASS_RE.match(stripped)
        if m_class:
            name = m_class.group(1)
            if name in seen_classes:
                duplicate_from = idx
                break
            seen_classes.add(name)
            continue

    if duplicate_from is not None:
        return lines[:duplicate_from]
    return lines


def detect_repetition_issues(code: str, language: str = "python") -> list[str]:
    if not code:
        return []

    issues: list[str] = []
    lines = [line.rstrip() for line in _strip_markdown_fences(code).splitlines()]
    non_empty = [line for line in lines if line.strip()]
    if not non_empty:
        return []

    repeated_start = _find_repeated_block_start(non_empty, min_block=3, min_match=8)
    if repeated_start is not None:
        issues.append("Detected repeated code block in output")

    repeated_runs = sum(1 for idx in range(1, len(non_empty)) if non_empty[idx] == non_empty[idx - 1])
    if repeated_runs >= 2:
        issues.append("Detected excessive repeated consecutive lines")

    if language.lower() == "python":
        guard_count = sum(1 for line in lines if MAIN_GUARD_RE.match(line.strip()))
        if guard_count > 1:
            issues.append("Detected multiple Python __main__ guard blocks")

        trimmed = _trim_python_restart_tail(lines)
        if len(trimmed) < len(lines):
            issues.append("Detected repeated top-level Python definitions")

    return issues


def sanitize_generated_code(code: Optional[str], language: str = "python") -> Optional[str]:
    if not code:
        return code

    cleaned = _strip_markdown_fences(code)
    lines = [line.rstrip() for line in cleaned.splitlines()]
    lines = _dedupe_consecutive_lines(lines, max_run=1)

    repeated_start = _find_repeated_block_start(lines, min_block=3, min_match=8)
    if repeated_start is not None:
        lines = lines[:repeated_start]

    if language.lower() == "python":
        lines = _trim_python_restart_tail(lines)
        lines = _keep_single_python_main_guard(lines)

    sanitized = "\n".join(lines).strip()
    return sanitized
