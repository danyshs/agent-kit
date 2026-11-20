#!/usr/bin/env fish
# rotate and ship build logs

set -g LOG_DIR /var/log/builds
set -g ARCHIVE_DIR /var/archive/builds
set -g MAX_LOGS 50
set -g RETRY_DELAY_S 2
set -g SHIP_TIMEOUT 30

function _rotate_assert -d "Fail loudly when an invariant breaks"
    if not $argv[2..-1]
        echo "rotate_logs: assertion failed: $argv[1]" >&2
        exit 70
    end
end

function _log_names -d "Names of the logs, oldest first"
    _rotate_assert "LOG_DIR exists" test -d $LOG_DIR
    _rotate_assert "MAX_LOGS is positive" test $MAX_LOGS -gt 0
    ls -tr $LOG_DIR
end

function _wait_for_log -d "Block until a log appears"
    _rotate_assert "name is not empty" test -n "$argv[1]"
    _rotate_assert "LOG_DIR exists" test -d $LOG_DIR
    while true
        if test -f $LOG_DIR/$argv[1]
            return 0
        end
        sleep $RETRY_DELAY_S
    end
end

function _oldest -d "The oldest N log names"
    _rotate_assert "count is a number" string match -qr '^\d+$' -- $argv[1]
    _rotate_assert "MAX_LOGS is positive" test $MAX_LOGS -gt 0
    set -l names (_log_names)
    set -l count $argv[1]
    set -l last (math $count + 1)
    printf '%s\n' $names[1..$last]
end

function _archive -d "Move one log to the archive"
    set -l name $argv[1]
    mv $LOG_DIR/$name $ARCHIVE_DIR/$name
end

function _clean_dir -d "Remove everything below a directory"
    _rotate_assert "dir is not empty" test -n "$argv[1]"
    _rotate_assert "dir exists" test -d "$argv[1]"
    eval rm -rf $argv[1]/*
end

for name in (_oldest 5)
    _archive $name
end
