#!/bin/sh
# "Your turn" beep, unique-ish per session so parallel sessions are told apart by ear.
# Tone shape and pitch come from a hash of session_id: same session, same beep, no
# registry of open sessions to keep in sync. Used by the Stop and Notification hooks.
# Synthesized by ffmpeg, so no dependency on Apple's /System/Library/Sounds.
set -u

shape_count=14
span=29          # half-semitone slots, -7..+7 semitones; x14 shapes = 406 beeps
base_hz=880      # A5, the middle slot
length_s=0.9
rate_hz=48000

session=$(jq -r '.session_id // "default"' 2>/dev/null || echo default)
session=$(printf '%s' "$session" | tr -cd 'A-Za-z0-9-')
[ -n "$session" ] || session=default

seed=$(printf '%s' "$session" | cksum | cut -d' ' -f1)
half=$((seed % span - span / 2))
# divide first so shape and pitch come from different parts of the hash
shape=$((seed / span % shape_count))

# no ffmpeg (so no ffplay either): Apple's chime on a Mac, else silence
fallback() {
	command -v afplay >/dev/null 2>&1 && exec afplay /System/Library/Sounds/Glass.aiff
	exit 0
}
command -v ffmpeg >/dev/null 2>&1 || fallback

# decaying sine starting at $2 seconds: note <hz> <start_s> <decay_per_s>
note() {
	echo "sin(2*PI*$1*(t-$2))*exp(-$3*(t-$2))*gte(t,$2)"
}

dir="${TMPDIR:-/tmp}/claude-beep"
mkdir -p "$dir"
out="$dir/$session.aiff"
if [ ! -f "$out" ]; then
	f=$(awk -v b="$base_hz" -v h="$half" 'BEGIN { printf "%.3f", b * 2 ^ (h / 24) }')
	x="2*PI*$f*t"
	case "$shape" in
	0) e="sin($x)*exp(-7*t)" ;; # pluck
	1) e="(sin($x)+0.6*sin($x*2.76)*exp(-3*t)+0.3*sin($x*5.4)*exp(-6*t))*exp(-3*t)*0.6" ;; # bell
	2) e="$(note "$f" 0 12)+$(note "$f*1.5" 0.15 8)" ;; # fifth up
	3) e="$(note "$f*1.5" 0 12)+$(note "$f" 0.15 8)" ;; # fifth down
	4) e="$(note "$f" 0 14)+$(note "$f*1.25" 0.12 14)+$(note "$f*1.5" 0.24 8)" ;; # arpeggio
	5) e="$(note "$f" 0 14)+$(note "$f" 0.18 8)" ;; # double tap
	6) e="sin(2*PI*$f*(t+t*t))*exp(-5*t)" ;; # chirp up
	7) e="sin(2*PI*$f*(t-0.4*t*t))*exp(-5*t)" ;; # chirp down
	8) e="sin($x+2*sin(2*PI*7*t))*exp(-4*t)" ;; # vibrato
	9) e="sin($x)*(0.6+0.4*sin(2*PI*14*t))*exp(-3*t)" ;; # tremolo
	10) e="(sin($x)+0.5*sin($x*2)+0.25*sin($x*3))*exp(-4*t)*0.55" ;; # organ
	11) e="(sin($x)+0.4*sin($x*4)*exp(-25*t))*exp(-9*t)" ;; # marimba
	12) e="(sin($x)+sin($x*3)/3+sin($x*5)/5)*exp(-6*t)*0.7" ;; # soft square
	13) e="$(note "$f*2" 0 12)+$(note "$f" 0.15 8)" ;; # octave drop
	*) e="sin($x)*exp(-7*t)" ;;
	esac
	# min(1,t*300): 3ms fade-in so the start doesn't click; 0.5 keeps stacked notes off the clip
	e="($e)*min(1,t*300)*0.5"
	# tmp + mv: Stop and Notification can fire at once and race on the cache
	tmp="$dir/$session.$$.aiff"
	if ffmpeg -nostdin -loglevel error -y \
		-f lavfi -i "aevalsrc='$e':s=$rate_hz:d=$length_s" "$tmp"; then
		mv -f "$tmp" "$out"
	else
		rm -f "$tmp"
		fallback
	fi
fi
# ffplay ships with ffmpeg and plays on macOS and Linux alike
# redirects: it still prints a line-clear escape at -loglevel quiet
exec ffplay -nodisp -autoexit -loglevel quiet "$out" </dev/null >/dev/null 2>&1
