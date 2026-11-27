# turn-sound

A short sound when Claude finishes or needs you, different per session so you can tell parallel sessions apart.

    claude plugin install turn-sound@agent-kit

Needs jq and ffmpeg for the per-session sounds (`brew install jq ffmpeg` on a Mac, `sudo apt install jq ffmpeg` on Ubuntu). Without ffmpeg, a Mac plays a system chime and Linux stays silent.

## Parts

| Part | File | When it runs |
|---|---|---|
| Claude Code hook | `scripts/beep.sh` | When Claude stops, and on a permission prompt or a question for you |

No rule, so it costs no context.

## Files it writes

Cached sounds in your temp folder.

Back to the [plugin list](../../README.md#plugin-docs).
