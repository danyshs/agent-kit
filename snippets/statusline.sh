#!/usr/bin/env bash
# Claude Code status line: dense, colored, multi-line. Reads session JSON on stdin.
# Needs: bash 3.2+, jq, git. Linux or macOS. Field reference: https://code.claude.com/docs/en/statusline

input=$(cat)

# ---------- colors ----------
R=$'\e[0m'; B=$'\e[1m'; D=$'\e[2m'
c() { printf '\e[38;5;%sm' "$1"; }
C_LBL=$(c 244)   # labels
C_SEP=$(c 238)   # separators
C_MODEL=$(c 141); C_DIR=$(c 75); C_GIT=$(c 114); C_ADD=$(c 78); C_DEL=$(c 203)
C_TOK=$(c 81); C_OUT=$(c 213); C_CW=$(c 179); C_CR=$(c 116)
C_COST=$(c 221); C_TIME=$(c 152); C_WARN=$(c 208); C_BAD=$(c 196); C_OK=$(c 82)
SEP=" ${C_SEP}│${R} "

# ---------- helpers (pure bash, no forks) ----------
hum() { # 1234567 -> 1.2M
    local n=${1:-0}
    if   (( n >= 1000000 )); then printf '%d.%dM' $((n/1000000)) $(( (n%1000000)/100000 ))
    elif (( n >= 1000 ));    then printf '%d.%dk' $((n/1000)) $(( (n%1000)/100 ))
    else printf '%d' "$n"; fi
}
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
    else c 78; fi
}
bar() { # bar <pct> <width>
    local p=${1%.*} w=${2:-10} f i out=""; p=${p:-0}; (( p > 100 )) && p=100; (( p < 0 )) && p=0
    f=$(( (p * w + 50) / 100 ))
    for (( i=0; i<w; i++ )); do (( i < f )) && out+='█' || out+='░'; done
    printf '%s%s%s%s' "$(pcol "$p")" "${out:0:f}" "${C_SEP}" "${out:f}${R}"
}
sec() { printf '%s' "${C_LBL}$1${R}"; }

# ---------- one jq call extracts everything ----------
IFS=$'\x1f' read -r model model_id cwd projdir wt_git repo_own repo_name ver style \
    cost dur_ms api_ms lines_add lines_del \
    ctx_in ctx_out ctx_size ctx_used ctx_rem cu_in cu_out cu_cw cu_cr over200k \
    fast effort thinking \
    r5 r5_at r7 r7_at rsp rsp_at \
    pc_warm pc_ttl pc_exp pc_req pc_miss pc_reb pc_hit pc_cw pc_missre pc_cold pc_cause \
    sess_id sess_name transcript vimmode agent pr_num pr_url pr_state wt_name wt_branch <<<"$(
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

# ===================== LINE 1: identity / where =====================
l1="${C_MODEL}${B}◆ ${model}${R}"
[[ -n $effort ]] && l1+=" ${C_LBL}effort${R} ${C_MODEL}${effort}${R}"
[[ $fast == true ]] && l1+=" ${C_COST}⚡fast${R}"
[[ $thinking == true ]] && l1+=" ${C_MODEL}✦think${R}"
l1+="${SEP}${C_DIR}${B}${dirname}${R}"
[[ -n $projdir && $projdir != "$cwd" ]] && l1+=" ${C_LBL}(proj ${projdir##*/})${R}"
[[ -n $repo_name ]] && l1+=" ${C_LBL}${repo_own}/${repo_name}${R}"
[[ -n $git_seg ]] && l1+="${SEP}${git_seg}"
[[ -n $wt_name ]] && l1+="${SEP}$(sec worktree) ${C_GIT}${wt_name}${R}${wt_branch:+ ${C_LBL}(${wt_branch})${R}}"
[[ -n $wt_git && -z $wt_name ]] && l1+="${SEP}$(sec worktree) ${C_GIT}${wt_git}${R}"
if [[ -n $pr_num ]]; then
    case $pr_state in approved) pc=$(c 78);; changes_requested) pc=$(c 203);; draft) pc=$(c 244);; *) pc=$(c 221);; esac
    l1+="${SEP}${C_LBL}PR${R} ${pc}#${pr_num}${pr_state:+ $pr_state}${R}"
fi
[[ -n $agent ]]     && l1+="${SEP}$(sec agent) ${C_MODEL}${agent}${R}"
[[ -n $vimmode ]]   && l1+="${SEP}$(sec vim) ${C_TIME}${vimmode}${R}"
[[ -n $style && $style != default ]] && l1+="${SEP}$(sec style) ${style}"
[[ -n $sess_name ]] && l1+="${SEP}${C_TIME}“${sess_name}”${R}"

# ===================== LINE 2: context window =====================
if [[ -z $ctx_used ]]; then
    ctx_used=0; (( ctx_size > 0 )) && ctx_used=$(( ctx_in * 100 / ctx_size ))
fi
[[ -z $ctx_rem ]] && ctx_rem=$(( 100 - ${ctx_used%.*} ))
l2="$(sec context) $(bar "$ctx_used" 20) $(pcol "$ctx_used")${B}${ctx_used%.*}%${R} ${C_LBL}(${R}$(hum "$ctx_in")${C_LBL}/${R}$(hum "$ctx_size")${C_LBL} used, ${ctx_rem%.*}% free, $(hum $(( ctx_size - ctx_in ))) left)${R}"
[[ $over200k == true ]] && l2+=" ${C_WARN}>200k${R}"
l2+="${SEP}$(sec 'last call') ${C_TOK}in $(hum "$cu_in")${R} ${C_OUT}out $(hum "$cu_out")${R} ${C_CW}cache-write $(hum "$cu_cw")${R} ${C_CR}cache-read $(hum "$cu_cr")${R}"

# ===================== LINE 3: session totals =====================
l3="$(sec session) ${B}$(hum "$tot_all")${R} ${C_LBL}tokens${R} ${C_LBL}(${R}${C_TOK}in $(hum "$tot_in")${R} ${C_OUT}out $(hum "$tot_out")${R} ${C_CW}cw $(hum "$tot_cw")${R} ${C_CR}cr $(hum "$tot_cr")${R}${C_LBL})${R} ${C_LBL}over${R} ${tot_calls} ${C_LBL}calls${R}"
cost_fmt=$(printf '%.2f' "$cost" 2>/dev/null || echo 0.00)
l3+="${SEP}${C_COST}${B}\$${cost_fmt}${R}"
dsec=$(( dur_ms / 1000 ))
if (( dsec >= 60 )); then
    # cost burn rate in dollars/hr; awk because bash has no float math
    burn=$(awk -v c="$cost" -v s="$dsec" 'BEGIN{printf "%.2f", c*3600/s}')
    l3+=" ${C_LBL}(\$${burn}/hr)${R}"
fi
l3+="${SEP}$(sec time) ${C_TIME}$(dur "$dsec")${R} ${C_LBL}wall${R} ${C_TIME}$(dur $(( api_ms / 1000 )))${R} ${C_LBL}api${R}"
(( dur_ms > 0 )) && l3+=" ${C_LBL}($(( api_ms * 100 / dur_ms ))% waiting on API)${R}"
l3+="${SEP}${C_ADD}+${lines_add}${R} ${C_DEL}-${lines_del}${R} ${C_LBL}lines${R}"

# ===================== LINE 4: usage limits + prompt cache =====================
l4=""
lim() { # lim <label> <pct> <resets_at>
    local p=${2%.*}; local rs=""
    [[ -n $3 ]] && rs=" ${C_LBL}resets in $(dur $(( $3 - now )))${R}"
    printf '%s %s %s%s%%%s%s' "$(sec "$1")" "$(bar "$p" 10)" "$(pcol "$p")${B}" "$p" "$R" "$rs"
}
[[ -n $r5  ]] && l4+="$(lim '5h limit' "$r5" "$r5_at")"
[[ -n $r7  ]] && l4+="${l4:+$SEP}$(lim '7d limit' "$r7" "$r7_at")"
[[ -n $rsp ]] && l4+="${l4:+$SEP}$(lim 'spend' "$rsp" "$rsp_at")"
[[ -z $l4 ]] && l4="$(sec limits) ${D}n/a (appears after first response on Pro/Max)${R}"
if [[ -n $pc_req ]]; then
    if [[ $pc_warm == true ]]; then
        pcs="${C_OK}warm${R}"; [[ -n $pc_exp ]] && pcs+=" ${C_LBL}${pc_ttl} ttl, cold in $(dur $(( pc_exp - now )))${R}"
    else pcs="${C_BAD}cold${R}"; fi
    l4+="${SEP}$(sec 'prompt cache') ${pcs}"
    [[ -n $pc_hit ]] && l4+=" ${C_LBL}hit${R} ${C_OK}$(awk -v h="$pc_hit" 'BEGIN{printf "%d", h*100}')%${R}"
    l4+=" ${C_LBL}req${R} ${pc_req} ${C_LBL}miss${R} ${pc_miss:-0}"
    [[ -n $pc_reb && $pc_reb != 0 ]] && l4+=" ${C_LBL}rebuilds${R} ${pc_reb}"
    [[ -n $pc_cause ]] && l4+=" ${C_WARN}(last: ${pc_cause})${R}"
    [[ -n $pc_cold ]] && l4+=" ${C_LBL}recache-if-cold${R} $(hum "$pc_cold")"
fi

# ===================== LINE 5: machine / meta =====================
if [[ -r /proc/loadavg ]]; then
    read -r host </proc/sys/kernel/hostname
    read -r ld1 ld5 ld15 _ </proc/loadavg
    memline=$(awk '/MemTotal/{t=$2}/MemAvailable/{a=$2}END{u=t-a; printf "%.1f/%.1fG %d%%", u/1048576, t/1048576, u*100/t}' /proc/meminfo)
else # macOS: used = active + wired + compressed, roughly Activity Monitor's "Memory Used"
    host=$(hostname -s)
    read -r _ ld1 ld5 ld15 _ <<<"$(sysctl -n vm.loadavg)"
    memline=$(vm_stat | awk -v t="$(sysctl -n hw.memsize)" '
        /page size of/{ps=$8} /^Pages active/{a=$3} /^Pages wired/{w=$4} /occupied by compressor/{c=$5}
        END{u=(a+w+c)*ps; printf "%.1f/%.1fG %d%%", u/1073741824, t/1073741824, u*100/t}')
fi
host=${host%%.*}
l5="$(sec host) ${C_DIR}${USER:-${LOGNAME:-?}}@${host}${R}${SEP}$(sec load) ${ld1} ${ld5} ${ld15}${SEP}$(sec mem) ${memline}"
l5+="${SEP}${C_TIME}$(date '+%a %d %b %H:%M:%S')${R}"
l5+="${SEP}$(sec claude) v${ver}${sess_id:+ ${D}${sess_id:0:8}${R}}"
[[ -n $model_id ]] && l5+=" ${D}${model_id}${R}"

printf '%s\n%s\n%s\n%s\n%s\n' "$l1" "$l2" "$l3" "$l4" "$l5"
