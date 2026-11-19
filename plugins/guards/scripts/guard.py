"""PreToolUse guards for Claude Code.

guard.py agent  -> on Agent calls: model ceiling, then the user's budget (typed, or a
                   proposal the user approved).
guard.py turn   -> on every main-agent call: the turn cap (10M tokens by default).

Exit 0 allows, exit 2 refuses with one stderr line. The hooks.json wrapper turns any
other status into 2. A crash refuses an Agent launch but lets other tool calls through. Standard library only; runs on python 3.9.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

MEGABYTE = 1024 * 1024
MAIN_BYTES_MAX = 128 * MEGABYTE  # real sessions stay well under this
SUBAGENT_FILES_MAX = 64  # most seen in one session 15
SUBAGENT_BYTES_MAX = 64 * MEGABYTE  # most seen in one session 13.4 MB
STDIN_BYTES_MAX = 16 * MEGABYTE  # a launch prompt is the big part
BLOCK_BYTES = MEGABYTE
TOKENS_PER_MILLION = 1_000_000
TURN_CAP_DEFAULT = 10 * TOKENS_PER_MILLION
TURN_CAP_MILLIONS_MAX = 100
RATE_NAMES = ("in", "cw5m", "cw1h", "cr", "out")
PRICES_PATH = Path(__file__).resolve().with_name("prices.json")
AGENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
BUDGET_PATTERN = re.compile(r"budget\s+(\$?\d+(\.\d{1,2})?|off)", re.IGNORECASE)
PROPOSAL_PATTERN = re.compile(r"proposed\s+budget\s+\$(\d+(\.\d{1,2})?)", re.IGNORECASE)
APPROVAL_PATTERN = re.compile(
    r"\b(yes|ok|okay|sure|approve|approved|go|go on|go ahead|continue|proceed|do it|keep going"
    r"|sounds good|lgtm)\b",
    re.IGNORECASE,
)
# any of these and it is not a plain yes: a doubt, a condition, or a number of the user's own
VETO_PATTERN = re.compile(r"\?|\$|\b(no|not|don't|dont|wait|but|stop|hold|cancel|instead)\b",
                          re.IGNORECASE)
APPROVAL_CHARS_MAX = 120  # real approvals are a few words
PROPOSAL_DOLLARS_MAX = 20.0  # over this the user types the line; a "yes" is too cheap for it
TURN_CAP_PATTERN = re.compile(r"turn\s+cap\s+(\d{1,3}m|off)", re.IGNORECASE)
SYNTHETIC_MODEL = "<synthetic>"

Record = Dict[str, object]


class Refusal(Exception):
    """A decision to refuse; the message is the whole stderr line after 'guard: '."""


# -- decoding: transcript lines are untrusted JSON, narrowed here and nowhere else


def field(value: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def text_of(content: object) -> Optional[str]:
    """String content, or the first text block of list content (images ride along)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if field(block, "type") == "text":
                text = field(block, "text")
                if isinstance(text, str):
                    return text
    return None


def typed_text(record: Record) -> Optional[str]:
    """The text of a message the user typed, else None."""
    if record.get("type") == "user":
        if field(record, "origin", "kind") != "human":
            return None
        if record.get("isMeta"):
            return None
        return text_of(field(record, "message", "content"))
    if record.get("type") == "attachment":
        if field(record, "attachment", "type") != "queued_command":
            return None
        if field(record, "attachment", "origin", "kind") != "human":
            return None
        return text_of(field(record, "attachment", "prompt"))
    return None


def first_line(text: str) -> str:
    return text.split("\n", 1)[0].strip()


def parse_line(raw: bytes, is_last: bool) -> Optional[Record]:
    """A half-written last line is skipped; a broken line anywhere else is an error."""
    try:
        value: object = json.loads(raw)
    except ValueError:
        if is_last:
            return None
        raise
    if not isinstance(value, dict):
        return None
    record: Record = {}
    for key, item in value.items():
        record[str(key)] = item
    return record


def timestamp_of(record: Record) -> datetime.datetime:
    stamp = record.get("timestamp")
    if not isinstance(stamp, str):
        raise ValueError("transcript line without a timestamp")
    # 3.9's fromisoformat does not take a trailing Z
    parsed = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp without a zone: {stamp}")
    return parsed


# -- usage: one message is written on several lines with one id; keep the largest per field


@dataclass
class Usage:
    model: str
    input_tokens: int
    cache_write_5m_tokens: int
    cache_write_1h_tokens: int
    cache_read_tokens: int
    output_tokens: int

    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
            + self.cache_read_tokens
            + self.output_tokens
        )


def count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    if value < 0:
        raise ValueError(f"negative token count {value}")  # transcript data, not an invariant
    return value


def usage_of(record: Record) -> Optional[Usage]:
    if record.get("type") != "assistant":
        return None
    model = field(record, "message", "model")
    usage = field(record, "message", "usage")
    if not isinstance(model, str) or model == SYNTHETIC_MODEL or not isinstance(usage, dict):
        return None
    # mid-stream lines can carry the TTL breakdown with a total of 0 (10 seen); trust the
    # breakdown, and count any excess in the total as 5-minute writes
    write_total = count(field(usage, "cache_creation_input_tokens"))
    write_1h = count(field(usage, "cache_creation", "ephemeral_1h_input_tokens"))
    write_5m = count(field(usage, "cache_creation", "ephemeral_5m_input_tokens"))
    return Usage(
        model=model,
        input_tokens=count(field(usage, "input_tokens")),
        cache_write_5m_tokens=max(write_5m, write_total - write_1h),
        cache_write_1h_tokens=write_1h,
        cache_read_tokens=count(field(usage, "cache_read_input_tokens")),
        output_tokens=count(field(usage, "output_tokens")),
    )


def dedup_usage(records: List[Record]) -> Dict[str, Usage]:
    by_id: Dict[str, Usage] = {}
    for record in records:
        usage = usage_of(record)
        message_id = field(record, "message", "id")
        if usage is None or not isinstance(message_id, str):
            continue
        seen = by_id.get(message_id)
        if seen is None:
            by_id[message_id] = usage
            continue
        seen.input_tokens = max(seen.input_tokens, usage.input_tokens)
        seen.cache_write_5m_tokens = max(seen.cache_write_5m_tokens, usage.cache_write_5m_tokens)
        seen.cache_write_1h_tokens = max(seen.cache_write_1h_tokens, usage.cache_write_1h_tokens)
        seen.cache_read_tokens = max(seen.cache_read_tokens, usage.cache_read_tokens)
        seen.output_tokens = max(seen.output_tokens, usage.output_tokens)
    return by_id


# -- reading: backwards in 1 MB blocks, so the common case reads only the current turn


def lines_reversed(path: Path, size_bytes: int) -> Iterator[bytes]:
    assert 0 <= size_bytes <= MAIN_BYTES_MAX, size_bytes
    with path.open("rb") as handle:
        position = size_bytes
        carry = b""
        blocks_max = size_bytes // BLOCK_BYTES + 1
        for _ in range(blocks_max):
            if position == 0:
                break
            read_bytes = min(BLOCK_BYTES, position)
            position -= read_bytes
            handle.seek(position)
            # \n never occurs inside a UTF-8 sequence, so a byte split cannot cut a character
            parts = (handle.read(read_bytes) + carry).split(b"\n")
            carry = parts[0]
            for part in reversed(parts[1:]):
                if part:
                    yield part
        assert position == 0, position
        if carry:
            yield carry


def checked_size(path: Path, limit_bytes: int) -> int:
    if not path.is_file():
        raise Refusal(f"refused: transcript missing: {path}; fix: restart the session")
    size_bytes = path.stat().st_size
    if size_bytes > limit_bytes:
        raise Refusal(
            f"refused: {path} is {size_bytes // MEGABYTE} MB, over the"
            f" {limit_bytes // MEGABYTE} MB limit; fix: /clear, or raise the limit in guard.py"
        )
    return size_bytes


def main_transcript(hook_input: Record) -> Path:
    value = hook_input.get("transcript_path")
    if not isinstance(value, str) or not value:
        raise Refusal("refused: hook input has no transcript_path; fix: check the hook setup")
    path = Path(value)
    # nested launches still get the main transcript (probed); a change must show, not miscount
    if path.parent.name == "subagents":
        raise Refusal(f"refused: transcript_path is a sub-agent's: {path}; fix: update guard.py")
    return path


# -- turn mode


def turn_cap_of(line: str) -> Optional[str]:
    """'off', a cap in tokens as digits, or None when the line is not a valid cap line."""
    match = TURN_CAP_PATTERN.fullmatch(line)
    if match is None:
        return None
    value = match.group(1).lower()
    if value == "off":
        return value
    millions = int(value[:-1])
    if millions < 1 or millions > TURN_CAP_MILLIONS_MAX:
        return None
    return str(millions * TOKENS_PER_MILLION)


def read_back(path: Path, size_bytes: int) -> List[Record]:
    """The current turn's records, plus the latest earlier typed numeric cap line, in file order."""
    kept: List[Record] = []
    turn_started = False
    for index, raw in enumerate(lines_reversed(path, size_bytes)):
        human = b'"human"' in raw
        if not human and (turn_started or b'"assistant"' not in raw):
            continue
        record = parse_line(raw, index == 0)
        if record is None:
            continue
        text = typed_text(record) if human else None
        cap = None if text is None else turn_cap_of(first_line(text))
        if turn_started:
            if cap is not None and cap != "off":
                kept.append(record)
                break
            continue
        kept.append(record)
        if text is not None:
            turn_started = True
            if cap is not None:
                break  # the turn start sets its own cap; nothing earlier matters
    kept.reverse()
    return kept


def turn_cap(records: List[Record]) -> Optional[int]:
    """Tokens allowed this turn; None = off. Off applies only to the turn it starts."""
    lines = [first_line(text) for text in map(typed_text, records) if text is not None]
    if lines and turn_cap_of(lines[-1]) == "off":
        return None
    for line in reversed(lines):
        cap = turn_cap_of(line)
        if cap is not None and cap != "off":
            return int(cap)
    return TURN_CAP_DEFAULT


def turn_tokens(records: List[Record]) -> int:
    start = 0
    for index, record in enumerate(records):
        if typed_text(record) is not None:
            start = index + 1
    tokens = sum(usage.total_tokens() for usage in dedup_usage(records[start:]).values())
    assert tokens >= 0, tokens
    return tokens


def run_turn(hook_input: Record) -> str:
    if hook_input.get("agent_id") is not None:
        return ""  # sub-agent calls are agent mode's, by budget
    path = main_transcript(hook_input)
    records = read_back(path, checked_size(path, MAIN_BYTES_MAX))
    cap = turn_cap(records)
    if cap is None:
        return ""
    tokens = turn_tokens(records)
    if tokens > cap:
        raise Refusal(
            f"turn cap: {tokens / TOKENS_PER_MILLION:.1f}M of {cap // TOKENS_PER_MILLION}M"
            f" tokens this turn; fix: user replies, or types 'turn cap <bigger>M'"
        )
    return ""


# -- prices


@dataclass(frozen=True)
class Prices:
    ceiling: str
    floor_tokens: int
    aliases: Dict[str, str]
    families: Dict[str, str]
    models: Dict[str, Dict[str, float]]


def string_map(value: object, name: str) -> Dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} is not an object")
    result: Dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ValueError(f"{name} holds a non-string entry")
        result[key] = item
    return result


def rate_table(value: object) -> Dict[str, Dict[str, float]]:
    if not isinstance(value, dict) or not value:
        raise ValueError("models is not a non-empty object")
    models: Dict[str, Dict[str, float]] = {}
    for model_id, rates in value.items():
        row: Dict[str, float] = {}
        for name in RATE_NAMES:
            rate = field(rates, name)
            if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate < 0:
                raise ValueError(f"models.{model_id}.{name} is not a rate")
            row[name] = float(rate)
        models[str(model_id)] = row
    return models


def load_prices(path: Path) -> Prices:
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
        prices = Prices(
            ceiling=str(field(data, "ceiling")),
            floor_tokens=count(field(data, "floor_tokens")),
            aliases=string_map(field(data, "aliases"), "aliases"),
            families=string_map(field(data, "families"), "families"),
            models=rate_table(field(data, "models")),
        )
        targets = [prices.ceiling, *prices.aliases.values(), *prices.families.values()]
        for target in targets:
            if target not in prices.models:
                raise ValueError(f"{target} has no rates")
        if prices.floor_tokens == 0:
            raise ValueError("floor_tokens is missing or zero")
    except (OSError, ValueError) as error:
        raise Refusal(f"refused: {path} unreadable: {error}; fix: correct {path}") from error
    return prices


@dataclass(frozen=True)
class LaunchRates:
    rates: Dict[str, float]
    priced: bool  # False: a newer id than the table, priced by its family


def rates_for_launch(model: str, prices: Prices) -> LaunchRates:
    model_id = prices.aliases.get(model, model)
    if model_id in prices.models:
        return LaunchRates(prices.models[model_id], True)
    for prefix, family_id in prices.families.items():
        if model_id.startswith(prefix):
            return LaunchRates(prices.models[family_id], False)
    raise Refusal(f"refused: unknown model {model}; fix: add it to {PRICES_PATH}")


def check_ceiling(model: str, rates: Dict[str, float], prices: Prices) -> None:
    ceiling = prices.models[prices.ceiling]
    for name in RATE_NAMES:
        if rates[name] > ceiling[name]:
            raise Refusal(
                f"refused: model {model} is above the ceiling {prices.ceiling}"
                f" ({name} {rates[name]} > {ceiling[name]} $/M); fix: name a model at or"
                f" below Opus in the launch"
            )


def floor_usd(rates: Dict[str, float], prices: Prices) -> float:
    return prices.floor_tokens * rates["cw5m"] / TOKENS_PER_MILLION


# -- agent mode


def latest_model(path: Path, limit_bytes: int) -> str:
    for index, raw in enumerate(lines_reversed(path, checked_size(path, limit_bytes))):
        if b'"assistant"' not in raw:
            continue
        record = parse_line(raw, index == 0)
        usage = None if record is None else usage_of(record)
        if usage is not None:
            return usage.model
    raise Refusal(f"refused: no model found in {path}; fix: name a model in the launch")


def resolve_model(hook_input: Record, main_path: Path) -> str:
    named = field(hook_input, "tool_input", "model")
    if isinstance(named, str) and named:
        return named
    # a launch that names no model runs the launcher's
    agent_id = hook_input.get("agent_id")
    if agent_id is None:
        return latest_model(main_path, MAIN_BYTES_MAX)
    if not isinstance(agent_id, str) or AGENT_ID_PATTERN.fullmatch(agent_id) is None:
        raise Refusal(f"refused: odd agent_id {agent_id!r}; fix: name a model in the launch")
    launcher = main_path.with_suffix("") / "subagents" / f"agent-{agent_id}.jsonl"
    return latest_model(launcher, SUBAGENT_BYTES_MAX)


@dataclass(frozen=True)
class Budget:
    dollars: Optional[float]  # None with off=False: no budget line at all
    off: bool
    since: Optional[datetime.datetime]
    approved: bool = False  # from approved proposals, not a typed line


def proposal_dollars(text: str) -> Optional[float]:
    """N from a reply whose last non-empty line is `proposed budget $N`, if 0 < N <= max."""
    lines = [line.strip().strip("`*").strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return None
    match = PROPOSAL_PATTERN.fullmatch(lines[-1])
    if match is None:
        return None
    dollars = float(match.group(1))
    if not 0 < dollars <= PROPOSAL_DOLLARS_MAX:
        return None
    assert dollars == round(dollars, 2), dollars  # the pattern allows two decimals at most
    return dollars


def assistant_text(record: Record) -> Optional[str]:
    """All text blocks of an agent line joined, else None (tool calls and thinking only)."""
    if record.get("type") != "assistant":
        return None
    content = field(record, "message", "content")
    if not isinstance(content, list):
        return None
    texts = [field(block, "text") for block in content if field(block, "type") == "text"]
    return "\n".join(text for text in texts if isinstance(text, str)) or None


def is_approval(text: str) -> bool:
    """A one-line reply with an approval word and nothing that doubts or conditions it."""
    reply = text.strip()
    if "\n" in reply or len(reply) > APPROVAL_CHARS_MAX:
        return False
    if VETO_PATTERN.search(reply):
        return False
    return APPROVAL_PATTERN.search(reply) is not None


@dataclass(frozen=True)
class Approval:
    dollars: float
    since: datetime.datetime


def scan_budget(main_path: Path, size_bytes: int) -> Tuple[Optional[Budget], List[Approval]]:
    """Newest typed budget line (stops the scan), plus approvals newer than it, newest first."""
    approvals: List[Approval] = []
    pending: Optional[Record] = None  # a typed approval word waiting for the agent text before it
    for index, raw in enumerate(lines_reversed(main_path, size_bytes)):
        if pending is not None and b'"assistant"' in raw:
            reply = parse_line(raw, index == 0)
            spoken = None if reply is None else assistant_text(reply)
            if spoken is not None:
                dollars = proposal_dollars(spoken)
                if dollars is not None:
                    assert typed_text(pending) is not None, pending
                    approvals.append(Approval(dollars, timestamp_of(pending)))
                pending = None  # only the nearest agent text can pair with it
        if b'"human"' not in raw:
            continue
        record = parse_line(raw, index == 0)
        text = None if record is None else typed_text(record)
        if record is None or text is None:
            continue
        pending = None  # a typed message between an approval word and the proposal breaks the pair
        if is_approval(text):
            pending = record
            continue
        match = BUDGET_PATTERN.fullmatch(first_line(text))
        if match is None:
            continue
        value = match.group(1).lower()
        if value == "off":
            return Budget(None, True, None), approvals
        dollars = float(value.lstrip("$"))
        if dollars > 0:
            return Budget(dollars, False, timestamp_of(record)), approvals
    return None, approvals


def budget_line(main_path: Path) -> Budget:
    """A typed line beats every proposal, in any order. Without one, approved amounts add up,
    capped at PROPOSAL_DOLLARS_MAX, with spend counted from the oldest approval, so a run of
    'go' replies never lets more than the cap through in one session."""
    size_bytes = checked_size(main_path, MAIN_BYTES_MAX)
    if size_bytes == 0:
        raise Refusal(f"refused: transcript empty: {main_path}; fix: restart the session")
    typed_budget, approvals = scan_budget(main_path, size_bytes)
    if typed_budget is not None:
        return typed_budget
    if not approvals:
        return Budget(None, False, None)
    newest, oldest = approvals[0], approvals[-1]
    assert oldest.since <= newest.since, (oldest, newest)
    dollars = min(sum(approval.dollars for approval in approvals), PROPOSAL_DOLLARS_MAX)
    assert 0 < dollars <= PROPOSAL_DOLLARS_MAX, dollars
    return Budget(dollars, False, oldest.since, approved=True)


def subagent_files(main_path: Path) -> List[Path]:
    directory = main_path.with_suffix("") / "subagents"
    if not directory.is_dir():
        return []
    paths = sorted(directory.glob("*.jsonl"))
    if len(paths) > SUBAGENT_FILES_MAX:
        raise Refusal(
            f"refused: {len(paths)} sub-agent transcripts, over {SUBAGENT_FILES_MAX};"
            f" fix: /clear, or raise the limit in guard.py"
        )
    total_bytes = sum(path.stat().st_size for path in paths)
    if total_bytes > SUBAGENT_BYTES_MAX:
        raise Refusal(
            f"refused: sub-agent transcripts total {total_bytes // MEGABYTE} MB, over"
            f" {SUBAGENT_BYTES_MAX // MEGABYTE} MB; fix: /clear, or raise the limit in guard.py"
        )
    return paths


@dataclass(frozen=True)
class Spend:
    dollars: float
    files: int
    messages: int
    unpriced: int


def spend_usd(paths: List[Path], since: datetime.datetime, prices: Prices) -> Spend:
    records: List[Record] = []
    for path in paths:
        raws = [raw for raw in path.read_bytes().split(b"\n") if raw]
        for index, raw in enumerate(raws):
            if b'"assistant"' not in raw:
                continue
            record = parse_line(raw, index == len(raws) - 1)
            # api-request-blob lines mention "assistant" and carry no timestamp
            if record is None or record.get("type") != "assistant":
                continue
            if timestamp_of(record) >= since:
                records.append(record)
    dollars = 0.0
    unpriced = 0
    usages = dedup_usage(records)
    for usage in usages.values():
        rates = prices.models.get(usage.model)
        if rates is None:
            rates = prices.models[prices.ceiling]  # unknown ids are priced high, never refused
            unpriced += 1
        dollars += (
            usage.input_tokens * rates["in"]
            + usage.cache_write_5m_tokens * rates["cw5m"]
            + usage.cache_write_1h_tokens * rates["cw1h"]
            + usage.cache_read_tokens * rates["cr"]
            + usage.output_tokens * rates["out"]
        ) / TOKENS_PER_MILLION
    assert dollars >= 0, dollars
    return Spend(dollars, len(paths), len(usages), unpriced)


def run_agent(hook_input: Record) -> str:
    main_path = main_transcript(hook_input)
    prices = load_prices(PRICES_PATH)
    model = resolve_model(hook_input, main_path)
    launch = rates_for_launch(model, prices)
    check_ceiling(model, launch.rates, prices)
    note = f"model {model}" + ("" if launch.priced else " (unpriced, family rates)")
    budget = budget_line(main_path)
    if budget.off:
        return f"guard: budget off; {note}"
    if budget.dollars is None or budget.since is None:
        return f"guard: no budget line; {note}"
    spend = spend_usd(subagent_files(main_path), budget.since, prices)
    floor = floor_usd(launch.rates, prices)
    if spend.dollars + floor > budget.dollars:
        raise Refusal(
            f"refused: spend ${spend.dollars:.2f} + launch floor ${floor:.2f} is over the budget;"
            f" spent ${spend.dollars:.2f} of ${budget.dollars:.2f}; fix: user types a higher"
            f" 'budget $N' line"
        )
    return (
        f"guard: ${spend.dollars:.2f} of ${budget.dollars:.2f} since"
        f" {'first approved proposal' if budget.approved else 'budget line'},"
        f" {spend.files} files, {spend.messages} messages, {spend.unpriced} unpriced; {note}"
    )


# -- entry


def read_hook_input() -> Record:
    raw = sys.stdin.buffer.read(STDIN_BYTES_MAX + 1)
    if len(raw) > STDIN_BYTES_MAX:
        raise Refusal("refused: hook input over 16 MB; fix: a shorter launch prompt")
    record = parse_line(raw, False)
    if record is None:
        raise Refusal("refused: hook input is not a JSON object; fix: check the hook setup")
    return record


def main(argv: List[str]) -> int:
    mode = argv[1] if len(argv) == 2 else ""
    try:
        if mode == "agent":
            output = run_agent(read_hook_input())
        elif mode == "turn":
            output = run_turn(read_hook_input())
        else:
            raise Refusal(f"refused: unknown mode {mode!r}; fix: use 'agent' or 'turn'")
    except Refusal as refusal:
        print(f"guard: {refusal}", file=sys.stderr)
        return 2
    except Exception as error:
        if mode == "turn":  # a broken turn cap must not lock every tool call
            print(f"guard: turn cap skipped, internal error: {type(error).__name__}: {error}", file=sys.stderr)
            return 0
        print(  # fail closed for launches: a crash refuses, with the error shown
            f"guard: refused: internal error: {type(error).__name__}: {error};"
            f" fix: see the guards plugin's scripts/guard.py, or /plugin disable guards",
            file=sys.stderr,
        )
        return 2
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
