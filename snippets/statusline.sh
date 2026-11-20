#!/usr/bin/env bash
# Claude Code status line: up to five lines, about 100 columns, colored. Reads session JSON on stdin.
# Needs: bash 3.2+, jq, git. Linux or macOS. Field reference: https://code.claude.com/docs/en/statusline

input=$(cat)

# ---------- colors ----------
R=$'\e[0m'; B=$'\e[1m'; D=$'\e[2m'
c() { printf '\e[38;5;%sm' "$1"; }
# roles: dim labels, cyan names, green healthy, amber spend/warning, lavender model, soft rose deletions, red only for broken
C_LBL=$(c 244); C_VAL=$(c 252); C_NAME=$(c 81); C_GIT=$C_NAME; C_MODEL=$(c 141)
C_ADD=$(c 78); C_DEL=$(c 174); C_OK=$(c 82); C_COST=$(c 221); C_WARN=$(c 179); C_BAD=$(c 196); C_TIME=$(c 152)

# ---------- helpers (pure bash, no forks) ----------
dur() { # seconds -> 1d2h / 3h05m / 4m07s / 9s
    local s=${1:-0}; (( s < 0 )) && s=0
    if   (( s >= 86400 )); then printf '%dd%dh' $((s/86400)) $(( (s%86400)/3600 ))
    elif (( s >= 3600 ));  then printf '%dh%02dm' $((s/3600)) $(( (s%3600)/60 ))
    elif (( s >= 60 ));    then printf '%dm%02ds' $((s/60)) $((s%60))
    else printf '%ds' "$s"; fi
}
pcol() { # percent -> color by severity (used%)
    local p=${1%.*}; p=${p:-0}
    if   (( p >= 90 )); then c 196
    elif (( p >= 75 )); then c 208
    elif (( p >= 50 )); then c 221
    else c 252; fi  # low is normal, not news: plain white, green stays for healthy states
}

# ---------- one jq call extracts everything ----------
IFS=$'\x1f' read -r model _ cwd _ wt_git repo_own repo_name ver style \
    cost dur_ms api_ms lines_add lines_del \
    ctx_in _ ctx_size ctx_used _ _ cu_out cu_cw cu_cr over200k \
    fast effort thinking \
    r5 r5_at r7 r7_at rsp rsp_at \
    pc_warm _ pc_exp pc_req pc_miss pc_reb pc_hit _ _ pc_cold pc_cause \
    sess_id sess_name transcript vimmode agent pr_num _ pr_state wt_name wt_branch <<<"$(
    jq -r '[
      (.model.display_name // "?"), (.model.id // ""),
      (.workspace.current_dir // .cwd // ""), (.workspace.project_dir // ""),
      (.workspace.git_worktree // ""), (.workspace.repo.owner // ""), (.workspace.repo.name // ""),
      (.version // ""), (.output_style.name // ""),
      (.cost.total_cost_usd // 0), (.cost.total_duration_ms // 0), (.cost.total_api_duration_ms // 0),
      (.cost.total_lines_added // 0), (.cost.total_lines_removed // 0),
      (.context_window.total_input_tokens // 0), (.context_window.total_output_tokens // 0),
      (.context_window.context_window_size // 0), (.context_window.used_percentage // ""),
      (.context_window.remaining_percentage // ""),
      (.context_window.current_usage.input_tokens // 0), (.context_window.current_usage.output_tokens // 0),
      (.context_window.current_usage.cache_creation_input_tokens // 0),
      (.context_window.current_usage.cache_read_input_tokens // 0),
      (.exceeds_200k_tokens // false),
      (.fast_mode // false), (.effort.level // ""), (.thinking.enabled // ""),
      (.rate_limits.five_hour.used_percentage // ""), (.rate_limits.five_hour.resets_at // ""),
      (.rate_limits.seven_day.used_percentage // ""), (.rate_limits.seven_day.resets_at // ""),
      (.rate_limits.spend_limit.used_percentage // ""), (.rate_limits.spend_limit.resets_at // ""),
      (.prompt_cache.warm // ""), (.prompt_cache.ttl // ""), (.prompt_cache.expires_at // ""),
      (.prompt_cache.requests // ""), (.prompt_cache.misses // ""), (.prompt_cache.expected_rebuilds // ""),
      (.prompt_cache.hit_ratio // ""), (.prompt_cache.cache_write_tokens // ""),
      (.prompt_cache.miss_recache_tokens // ""), (.prompt_cache.recache_tokens_if_cold // ""),
      ((.prompt_cache.last_miss_cause.causes // []) | join("+")),
      (.session_id // ""), (.session_name // ""), (.transcript_path // ""),
      (.vim.mode // ""), (.agent.name // ""),
      (.pr.number // ""), (.pr.url // ""), (.pr.review_state // ""),
      (.worktree.name // ""), (.worktree.branch // "")
    ] | map(tostring) | join("")' <<<"$input"
)"

# ---------- cumulative session token totals from the transcript ----------
tot_in=0 tot_out=0 tot_cw=0 tot_cr=0 tot_calls=0
if [[ -n $transcript && -r $transcript ]]; then
    cache="${XDG_CACHE_HOME:-$HOME/.cache}/claude-statusline"; mkdir -p "$cache" 2>/dev/null
    # GNU first: on Linux `stat -f` is filesystem status and would "succeed" with junk
    cf="$cache/${sess_id:-x}.tot"; sz=$(stat -c %s "$transcript" 2>/dev/null || stat -f %z "$transcript" 2>/dev/null)
    if [[ -r $cf ]] && read -r csz cin cout ccw ccr ccalls <"$cf" && [[ $csz == "$sz" ]]; then
        tot_in=$cin tot_out=$cout tot_cw=$ccw tot_cr=$ccr tot_calls=$ccalls
    else
        read -r tot_in tot_out tot_cw tot_cr tot_calls < <(
            jq -sr '[.[] | select(.message.usage != null)] | unique_by(.message.id) as $m
              | [ ($m|map(.message.usage.input_tokens // 0)|add // 0),
                  ($m|map(.message.usage.output_tokens // 0)|add // 0),
                  ($m|map(.message.usage.cache_creation_input_tokens // 0)|add // 0),
                  ($m|map(.message.usage.cache_read_input_tokens // 0)|add // 0),
                  ($m|length) ] | join(" ")' "$transcript" 2>/dev/null
        )
        : "${tot_in:=0}" "${tot_out:=0}" "${tot_cw:=0}" "${tot_cr:=0}" "${tot_calls:=0}"
        echo "$sz $tot_in $tot_out $tot_cw $tot_cr $tot_calls" >"$cf" 2>/dev/null
    fi
fi
tot_all=$(( tot_in + tot_out + tot_cw + tot_cr ))

now=$(date +%s) # not $EPOCHSECONDS, macOS /bin/bash is 3.2
dirname=${cwd##*/}; [[ -z $dirname ]] && dirname=/

# ---------- git ----------
git_seg=""
if [[ -n $cwd ]] && gs=$(git -C "$cwd" status --porcelain=v2 --branch 2>/dev/null); then
    br="" ahead=0 behind=0 staged=0 mod=0 untr=0 conf=0
    while IFS= read -r l; do
        case $l in
            '# branch.head '*) br=${l#\# branch.head } ;;
            '# branch.ab '*)   read -r _ _ a b <<<"$l"; ahead=${a#+}; behind=${b#-} ;;
            '1 '*|'2 '*)       x=${l:2:1}; y=${l:3:1}
                               [[ $x != . ]] && ((staged++)); [[ $y != . ]] && ((mod++)) ;;
            'u '*)             ((conf++)) ;;
            '? '*)             ((untr++)) ;;
        esac
    done <<<"$gs"
    git_seg="${C_GIT}${B}⎇ ${br}${R}"
    (( staged )) && git_seg+=" ${C_ADD}●${staged}${R}"
    (( mod ))    && git_seg+=" ${C_WARN}✚${mod}${R}"
    (( untr ))   && git_seg+=" ${C_LBL}…${untr}${R}"
    (( conf ))   && git_seg+=" ${C_BAD}✖${conf}${R}"
    (( ahead ))  && git_seg+=" ${C_OK}↑${ahead}${R}"
    (( behind )) && git_seg+=" ${C_WARN}↓${behind}${R}"
    (( ! staged && ! mod && ! untr && ! conf )) && git_seg+=" ${C_OK}✓${R}"
fi

# ===================== render =====================
fg() { printf '\e[38;5;%sm' "$1"; }
bg() { printf '\e[48;5;%sm' "$1"; }
pill() { printf '%s%s%s %s %s' "$(bg "$1")" "$(fg "$2")" "$B" "$3" "$R"; } # pill <bg> <fg> <text>
hum() { # 1234567 -> 1.2M, 1000000 -> 1M
    local n=${1:-0} s
    if   (( n >= 1000000 )); then s="$((n/1000000)).$(( (n%1000000)/100000 ))M"
    elif (( n >= 1000 ));    then s="$((n/1000)).$(( (n%1000)/100 ))k"
    else s=$n; fi
    printf '%s' "${s/.0/}"
}
gauge() { # gauge <pct> <width>: whole fill one colour, the same severity colour as its percentage; eighth-block tip
    local p=${1%.*} w=$2 i e col out tips=(' ' '▏' '▎' '▍' '▌' '▋' '▊' '▉')
    p=${p:-0}; (( p > 100 )) && p=100; (( p < 0 )) && p=0
    e=$(( p * w * 8 / 100 )); col=$(pcol "$p")
    out="$(bg 238)${col}"  # track lighter than the terminal bg, so an empty gauge still reads as a gauge
    for (( i=0; i<w; i++ )); do
        if   (( e >= (i+1)*8 )); then out+="█"
        elif (( e > i*8 ));      then out+="${tips[e - i*8]}"
        else out+=' '; fi
    done
    printf '%s%s' "$out" "$R"
}
lbl() { printf '%s%s%s' "$C_LBL" "$1" "$R"; }
val() { printf '%s%s%s' "$C_VAL" "$1" "$R"; }
pct() { printf '%s%s%3d%%%s' "$(pcol "$1")" "$B" "${1%.*}" "$R"; }  # fixed width so neighbours don't shift
G="  "

# --- grid: every line puts its cells on the same column stops ---
shopt -s extglob
W=$(( ${COLUMNS:-0} - 6 ))  # Claude Code sets COLUMNS but draws inside a narrower box and cuts with …; 6 spare
STOPS_PCT=(0 34 65)          # column starts as % of W; right column is widest, it holds the token breakdown
vis() { # visible width: strip SGR codes, count chars in a UTF-8 locale
    local LC_ALL=en_US.UTF-8 s=${1//$'\e'\[*([0-9;])m/}
    printf '%d' "${#s}"
}
grid() { # grid <cell>...: cell i starts at stop i, or 3 past the previous cell when it overran or the screen is narrow
    local out="" col=0 i=0 stop cell
    for cell in "$@"; do
        if (( i == 0 )); then stop=0
        elif (( W >= 90 && i < ${#STOPS_PCT[@]} )); then stop=$(( W * STOPS_PCT[i] / 100 )); (( stop < col + 3 )) && stop=$(( col + 3 ))
        else stop=$(( col + 3 )); fi
        [[ -z $cell ]] && { ((i++)); continue; }
        (( stop > col )) && out+=$(printf '%*s' $(( stop - col )) '')
        out+=$cell; col=$(( stop + $(vis "$cell") )); ((i++))
    done
    printf '%s' "$out"
}

# --- line 1: who and where | repo | session name ---
mtxt=$model; [[ -n $effort ]] && mtxt+=" · $effort"
a="$(pill 141 16 "$mtxt")"
[[ $fast == true ]] && a+=" $(pill 221 16 '⚡ fast')"
[[ $thinking == true ]] && a+=" ${C_MODEL}✦ thinking${R}"  # extended thinking on
# owner/repo; fall back to origin's URL when Claude Code doesn't send workspace.repo
if [[ -z $repo_name && -n $cwd ]] && url=$(git -C "$cwd" remote get-url origin 2>/dev/null); then
    url=${url%.git}; repo_name=${url##*/}; url=${url%/*}; repo_own=${url##*[:/]}
fi
if [[ -n $repo_name ]]; then
    b="${C_LBL}${repo_own:+${repo_own}/}${R}${C_NAME}${B}${repo_name}${R}"
    [[ $dirname != "$repo_name" ]] && b+=" $(lbl ›) ${C_NAME}${dirname}${R}"  # cwd is a subdir or a differently named checkout
else
    b="${C_NAME}${B}${dirname}${R}"
fi
[[ -n $git_seg ]] && b+="${G}${git_seg}"
[[ -n $wt_name ]] && b+="${G}$(lbl wt) ${C_NAME}${wt_name}${R}${wt_branch:+ $(lbl "(${wt_branch})")}"
[[ -n $wt_git && -z $wt_name ]] && b+="${G}$(lbl wt) ${C_NAME}${wt_git}${R}"
if [[ -n $pr_num ]]; then
    case $pr_state in approved) pc=78;; changes_requested) pc=174;; draft) pc=244;; *) pc=221;; esac
    b+="${G}$(fg $pc)PR #${pr_num}${pr_state:+ $pr_state}${R}"
fi
[[ -n $agent ]]   && b+="${G}$(lbl agent) ${C_MODEL}${agent}${R}"
[[ -n $vimmode ]] && b+="${G}$(val "$vimmode")"
[[ -n $style && $style != default ]] && b+="${G}$(lbl style) $(val "$style")"
z=""
# shellcheck disable=SC1111  # the curly quotes are shown text, not shell quoting
[[ -n $sess_name ]] && z="${C_TIME}“${sess_name}”${R}"
l1=$(grid "$a" "$b" "$z")

# --- line 2: context | 5h | 7d, each label gauge pct detail ---
if [[ -z $ctx_used ]]; then ctx_used=0; (( ctx_size > 0 )) && ctx_used=$(( ctx_in * 100 / ctx_size )); fi
a="$(lbl ctx) $(pct "$ctx_used") $(val "$(hum "$ctx_in")")$(lbl "/$(hum "$ctx_size")")"
[[ $over200k == true ]] && a+=" ${C_WARN}>200k${R}"
lim() { # lim <label> <pct> <resets_at>
    printf '%s %s %s' "$(lbl "$1")" "$(gauge "$2" 10)" "$(pct "$2")"
    [[ -n $3 ]] && printf ' %s' "$(lbl "↻$(dur $(( $3 - now )))")"
}
b=""; [[ -n $r5 ]] && b=$(lim 5h "$r5" "$r5_at")
z=""; [[ -n $r7 ]] && z=$(lim 7d "$r7" "$r7_at")
[[ -n $rsp ]] && z+="${z:+$G}$(lim spend "$rsp" "$rsp_at")"
l2=$(grid "$a" "$b" "$z")

# --- line 3: time and lines | cost and calls | session tokens ---
dsec=$(( dur_ms / 1000 ))
a="$(lbl time) $(val "$(dur "$dsec")")"
(( dur_ms > 0 )) && a+="${G}$(lbl "$(( api_ms * 100 / dur_ms ))% api")"
a+="${G}${C_ADD}+${lines_add}${R} ${C_DEL}−${lines_del}${R}"
b="$(lbl session) ${B}$(val "$(hum "$tot_all")")${G}$(lbl in) $(val "$(hum "$tot_in")")${G}$(lbl out) $(val "$(hum "$tot_out")")${G}$(lbl w) $(val "$(hum "$tot_cw")")${G}$(lbl r) $(val "$(hum "$tot_cr")")"
cost_fmt=$(printf '%.2f' "$cost" 2>/dev/null || echo 0.00)
z="${C_COST}${B}\$${cost_fmt}${R}"
# burn rate in dollars/hr; awk because bash has no float math
(( dsec >= 60 )) && z+=" $(lbl "\$$(awk -v c="$cost" -v s="$dsec" 'BEGIN{printf "%.2f", c*3600/s}')/h")"
z+="${G}$(lbl "${tot_calls} calls")"
l3=$(grid "$a" "$z" "$b")

# --- line 4: prompt cache | recache cost | last call ---
a="" b=""
if [[ -n $pc_req ]]; then
    if [[ $pc_warm == true ]]; then
        a="$(lbl cache) ${C_OK}● warm${R}"
        [[ -n $pc_exp ]] && a+=" $(lbl "cold in $(dur $(( pc_exp - now )))")"
    else a="$(lbl cache) ${C_BAD}● cold${R}"; fi
    [[ -n $pc_hit ]] && a+="${G}${C_OK}$(awk -v h="$pc_hit" 'BEGIN{printf "%d", h*100}')%${R} $(lbl hit)"
    b="$(lbl "${pc_miss:-0}/${pc_req} miss")"  # misses sit with the recache cost, keeps cell 1 inside its column
    [[ -n $pc_cause ]] && b+=" ${C_WARN}${pc_cause}${R}"
    [[ -n $pc_reb && $pc_reb != 0 ]] && b+=" $(lbl "${pc_reb} rebuilds")"
    [[ -n $pc_cold ]] && b+="${G}$(lbl "recache if cold") $(val "$(hum "$pc_cold")")"
fi
z="$(lbl 'last call')${G}$(lbl out) $(val "$(hum "$cu_out")")${G}$(lbl w) $(val "$(hum "$cu_cw")")${G}$(lbl r) $(val "$(hum "$cu_cr")")"
l4=$(grid "$a" "$b" "$z")

# --- line 5: machine | clock | version ---
if [[ -r /proc/loadavg ]]; then
    read -r host </proc/sys/kernel/hostname
    read -r ld1 _ </proc/loadavg
    memp=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{printf "%d", (t-a)*100/t}' /proc/meminfo)
else # macOS: used = active + wired + compressed, roughly Activity Monitor's "Memory Used"
    host=$(hostname -s)
    read -r _ ld1 _ <<<"$(sysctl -n vm.loadavg)"
    memp=$(vm_stat | awk -v t="$(sysctl -n hw.memsize)" '
        /page size of/{ps=$8} /^Pages active/{a=$3} /^Pages wired/{w=$4} /occupied by compressor/{c=$5}
        END{printf "%d", (a+w+c)*ps*100/t}')
fi
host=${host%%.*}
a="${C_NAME}${USER:-${LOGNAME:-?}}@${host}${R}${G}$(lbl load) $(val "$ld1")${G}$(lbl mem) $(pcol "$memp")${memp}%${R}"
b=$(val "$(date '+%a %d %b %H:%M:%S')")
z="${D}v${ver}${sess_id:+ ${sess_id:0:8}}${R}"
l5=$(grid "$a" "$b" "$z")

printf '%s\n%s\n%s\n%s\n%s\n' "$l1" "$l2" "$l3" "$l4" "$l5"
