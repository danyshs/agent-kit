"""Constants only. A later stage adds its own; a detector constant change bumps its version."""

from __future__ import annotations

from typing import Final

# bounds on every read and listing
SESSIONS_MAX: Final = 5_000
LINES_PER_FILE_MAX: Final = 200_000
FILE_BYTES_MAX: Final = 67_108_864
SUB_FILES_MAX: Final = 512
STEPS_PER_SESSION_MAX: Final = 100_000
BLOCKS_PER_MESSAGE_MAX: Final = 256
SUMMARIES_PER_EVENT_MAX: Final = 64
COMMAND_CHARS_SCANNED_MAX: Final = 8_192
SHAPE_SEGMENTS_MAX: Final = 16
# turns need no steps: one depth 0 turn per execution in the main file (at most its lines)
# plus one depth 1 turn per sub-file
TURNS_PER_SESSION_MAX: Final = LINES_PER_FILE_MAX + SUB_FILES_MAX
EVENTS_LINES_MAX: Final = 1 + TURNS_PER_SESSION_MAX + STEPS_PER_SESSION_MAX
STORE_ENTRIES_MAX: Final = 2 * SESSIONS_MAX  # events files plus stray names in one directory
CWDS_PER_SESSION_MAX: Final = 64
EVENTS_BYTES_MAX: Final = 402_653_184  # 384 MiB: the largest valid session (test_store) is ~401.4M
EXIT_TAIL_CHARS: Final = 64  # the code is the last line; never read more of the output than this
NAME_CHARS_MAX: Final = 64
SESSION_LINE_BYTES_MAX: Final = 8_192  # the worst session line is under 1 KiB (test_store)

# versions: any change to the matching logic bumps its number
SCHEMA_VERSION: Final = 1
READER_VERSION: Final = 1
RULE_VERSION: Final = 1

OTHER: Final = "(other)"

# closed vocabularies
HARNESSES: Final = ("kiro", "claude")
TREES: Final = {"kiro": ("interactive", "workflow"), "claude": ("main",)}
TOOL_CLASSES: Final = ("read", "write", "shell", "search", "web", "skill", "agent", "other")
# frozen after the stage 1 re-read of the Claude fields (stage_notes/stage1.md)
STATUS_VALUES: Final = ("ok", "error", "denied", "cancelled", "unpaired")
FLAG_NAMES: Final = ("commit_trailer", "git_no_verify", "heredoc", "shell_wrapper")
KIRO_SCHEMA_VERSIONS: Final = frozenset({"1.0.0"})
KIRO_DATA_MODEL_VERSIONS: Final = frozenset({1})

SHAPE_PROGRAMS: Final = frozenset(
    "git gh bash sh zsh fish sed awk python python3 uv ruff pyright jq rm cp mv cat echo printf "
    "cd ls find grep rg curl make npm node cargo swift xcodebuild brew".split()
)
SHAPE_SUBCOMMANDS: Final = {
    "git": frozenset(
        "commit push pull fetch checkout reset rebase merge stash add status diff log branch "
        "clone tag".split()
    ),
    "gh": frozenset("pr issue release repo api auth".split()),
}
SHAPE_OPTIONS: Final = frozenset(
    "--no-verify --force --force-with-lease -f -n -c -lc -F -m --amend --hard -i -rf -fr".split()
)

BACKUP_DIR_DEFAULT: Final = "~/.traj-backup/events"

# v2 step 1: the weekly correction list (docs/designs/trajectory-analysis/v2/design_draft.md)
CLAUDE_PROJECTS_DEFAULT: Final = "~/.claude/projects"
CORRECTION_FILES_MAX: Final = 20_000  # 101 on 2026-11-15; a year at 365-day retention fits
CORRECTION_QUOTE_CHARS_MAX: Final = 200
CORRECTION_TEXT_CHARS_MAX: Final = 400  # longer typed messages are briefs, not corrections
CORRECTION_ENTRIES_MAX: Final = 500  # about 50 a week measured

# stage 3: report and cost
TOP_TURNS: Final = 10
# versions for the report header; bumped at their respective stages
DETECTOR_VERSION: Final = 1  # stage 4 calibration may change this

# stage 2: Kiro reader, quality gate, build command
SESSION_JSON_BYTES_MAX: Final = 1_048_576  # a session.json is a few KiB; bound the read anyway
STATE_FILE_BYTES_MAX: Final = 16_777_216  # skips.jsonl and coverage.json, read back by report
CLI_LINES_MAX: Final = 1_000_000
CLI_BYTES_MAX: Final = 268_435_456
STAMP_CHARS_MAX: Final = 64
QUALITY_MALFORMED_MAX_PERCENT: Final = 1
QUALITY_UNKNOWN_MAX_PERCENT: Final = 1

# skip reasons, in precedence order: a session gets the first one that applies
SKIP_REASONS: Final = (
    "headless tree not read in v1",
    "unreadable",
    "no session.json",
    "no messages file",
    "eval traffic",
    "no cwd",
    "unknown format",
    "over bound",
)

# required payload keys per used Kiro event type; the time is the envelope `timestamp`
REQUIRED_KIRO_KEYS: Final = {
    "tool_call": ("toolCallId", "toolName", "args", "executionId", "status"),
    "tool_result": ("toolCallId", "success", "executionId", "content"),
    "usage_summary": ("executionId", "promptTurnSummaries"),
    "turn_start": ("executionId",),
    "turn_end": ("executionId", "stopReason"),
    "pending_interaction": ("toolCallId",),
    "interaction_resolved": ("toolCallId",),
}
# complete as of 2026-11-14; any other type adds 1 to unknown_events
KIRO_IGNORED_TYPES: Final = frozenset(
    "assistant user session_metadata session_event ContextualHookInvoked agent_note "
    "sub_agent_start sub_agent_complete steering_inclusion session_start tombstone".split()
)

# Claude reader (design_draft.md, "Claude reader"); the time is the line's `timestamp`
REQUIRED_CLAUDE_KEYS: Final = {
    "assistant": ("uuid", "timestamp", "message"),
    "user": ("uuid", "timestamp", "message"),
}
# re-read 2026-11-20 over the live store; any other type adds 1 to unknown_events
CLAUDE_IGNORED_TYPES: Final = frozenset(
    "attachment api-request api-request-blob api-request-shape last-prompt atis-latch mode "
    "ai-title permission-mode system file-history-snapshot file-history-delta queue-operation "
    "cost-state agent-name tools-used custom-title pr-link worktree-state relocated frame-link "
    "continued-in artifact-comment-monitor artifact-autoreact-ledger session-owner "
    "repl-mode".split()
)
CLAUDE_MALFORMED_MAX_PERCENT: Final = 1
CLAUDE_HEAD_LINES_MAX: Final = 256  # cwd and timestamp sit in the first few lines
CLAUDE_HEAD_BYTES_MAX: Final = 1_048_576

# keyed on the cleaned tool name; any other name is `other`
TOOL_CLASS: Final = {
    **dict.fromkeys(("read_file", "read_files", "Read"), "read"),
    **dict.fromkeys(
        ("str_replace", "fs_write", "fs_append", "delete_file", "Edit", "Write"), "write"
    ),
    **dict.fromkeys(("execute_bash", "Bash"), "shell"),
    **dict.fromkeys(
        ("grep_search", "file_search", "list_directory", "code", "Grep", "Glob", "ToolSearch"),
        "search",
    ),
    **dict.fromkeys(("web_fetch", "remote_web_search", "WebFetch", "WebSearch"), "web"),
    **dict.fromkeys(("disclose_context", "load_resource", "tool_load", "Skill"), "skill"),
    **dict.fromkeys(
        (
            "invoke_sub_agent",
            "orchestrate_subagent",
            "subagent_coder",
            "subagent_planner",
            "subagent_response",
            "run_workflow",
            "send_message",
            "Agent",
            "Task",
            "SubagentHandback",
            "SendMessage",
            "TaskStop",
            "ListAgents",
        ),
        "agent",
    ),
}
