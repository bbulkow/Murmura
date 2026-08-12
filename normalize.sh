#!/usr/bin/env bash
#
# Level-match audio for MUR playback. POSIX-shell port of normalize.ps1; same
# targets, same clamps, same output. Use either.
#
# gain mode (default)
#
#   Pass 1 measures the mono signal. Pass 2 applies ONE static gain, the
#   smallest of three:
#
#       gain = min( TargetI  - measured_I  ,      reach the loudness target
#                   TargetTP + PeakTolerance - measured_TP ,   intersample
#                   SampleCeiling - measured_SP )              hard clip guard
#
#   The first reaches the loudness target. The second holds true (intersample)
#   peaks to the ceiling plus its tolerance - note it does NOT hold them to
#   TargetTP itself. The third is absolute: an s16 write truncates anything at
#   or above 0 dBFS, and true peak does not predict that, because on a master
#   limited flat to full scale sample peak and true peak differ by several dB.
#
#   The smallest wins, so a track lands BELOW the loudness target when a peak
#   ceiling says it must. That is why loudnorm is not used: its second pass
#   refuses to undershoot, so when a flat gain would breach the ceiling it
#   switches to dynamic mode and compresses. Nearly every commercial master
#   peaks close to 0 dBFS, so nearly every file took that path.
#
#   A track quieter than the target by more than -undershoot-warn is flagged
#   QUIET. It CANNOT be raised at playback: MUR track volume is 0-100 mapped to
#   20*log10(v/100), so 100 is unity and there is no gain above it, and every
#   file already sits against the peak ceiling anyway. To match the set, lower
#   the rest - re-run with -target-i reduced by the worst shortfall, which the
#   summary prints for you.
#
# loudnorm mode
#
#   The original two-pass loudnorm, kept for comparison. Reports how much
#   dynamic range the compressor actually removed rather than that it ran.
#
# Channel handling: sum to mono in the filter chain, duplicate to stereo with
# -ac 2. A MUR drives one speaker, but its player reads a mono file as
# interleaved stereo frames and plays it at 2x speed.

set -u -o pipefail
export LC_ALL=C          # decimal points, not commas, in awk and printf

mode=gain
dest=norm2
target_i=-14.0
target_tp=-1.0
peak_tolerance=1.5
sample_ceiling=-0.1
target_lra=11.0
undershoot_warn=1.0
lra_warn=1.0
verify=0

usage() {
  sed -n '2,45p' "$0" | sed 's/^# \{0,1\}//'
  cat <<EOF

Usage: $(basename "$0") [options]

  --mode gain|loudnorm      default: gain
  --dest DIR                output directory, default: norm2
  --target-i LUFS           default: -14.0
  --target-tp dBFS          default: -1.0
  --peak-tolerance dB       default: 1.5
  --sample-ceiling dBFS     default: -0.1
  --target-lra LU           default: 11.0   (loudnorm mode only)
  --undershoot-warn LU      default: 1.0
  --lra-warn LU             default: 1.0
  --verify                  re-measure each written file
EOF
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --mode)             mode=$2; shift 2 ;;
    --dest)             dest=$2; shift 2 ;;
    --target-i)         target_i=$2; shift 2 ;;
    --target-tp)        target_tp=$2; shift 2 ;;
    --peak-tolerance)   peak_tolerance=$2; shift 2 ;;
    --sample-ceiling)   sample_ceiling=$2; shift 2 ;;
    --target-lra)       target_lra=$2; shift 2 ;;
    --undershoot-warn)  undershoot_warn=$2; shift 2 ;;
    --lra-warn)         lra_warn=$2; shift 2 ;;
    --verify)           verify=1; shift ;;
    -h|--help)          usage ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

case "$mode" in gain|loudnorm) ;; *) echo "--mode must be gain or loudnorm" >&2; exit 2 ;; esac
for tool in ffmpeg ffprobe awk; do
  command -v "$tool" >/dev/null 2>&1 || { echo "$tool not found in PATH" >&2; exit 2; }
done

# --- float helpers. awk rather than bc: it is always present, and it formats. --
calc() { awk -v a="$1" -v b="${2:-0}" -v c="${3:-0}" "BEGIN{printf \"%.10g\", $4}"; }
f()    { awk -v v="$1" -v n="${2:-2}" 'BEGIN{printf "%.*f", n, v}'; }
gt()   { awk -v a="$1" -v b="$2" 'BEGIN{exit !(a>b)}'; }
min3() { awk -v a="$1" -v b="$2" -v c="$3" 'BEGIN{m=a; if(b<m)m=b; if(c<m)m=c; printf "%.10g", m}'; }

root=$(cd "$(dirname "$0")" && pwd)
dst="$root/$dest"
mkdir -p "$dst"

log_path="$dst/normalize.log"
csv_path="$dst/normalize.csv"
log() { printf '%s\n' "$1" >>"$log_path"; }
warn() { printf 'WARNING: %s\n' "$1" >&2; log "$1"; }

pre="aformat=channel_layouts=mono"
bad_format=0
skipped=0
clipped=0
written=0
total=0

# The two targets together allow a fixed distance between a track's average
# level and its peaks. A track whose peaks sit further above its average than
# this cannot satisfy both, and lands quiet by the difference.
budget=$(calc "$target_tp" "$peak_tolerance" "$target_i" '(a+b)-c')

log ""
log "=== $(date '+%Y-%m-%d %H:%M:%S')  mode=$mode  I=$target_i TP=$target_tp LRA=$target_lra  -> $dest ==="

printf 'file,in_i,in_tp,in_sp,in_lra,plr,budget,gain_db,bound_by,out_i,out_tp,out_lra,sample_peak,quieter_by_db,lra_removed_lu,type,format,flag\n' >"$csv_path"
csv() { printf '%s\n' "$1" >>"$csv_path"; }

hdr_fmt='  %-30s %7s %7s   %5s    %6s   %7s %6s %6s   %5s  %s\n'
if [ "$mode" = gain ]; then
  echo
  echo "Targets: $target_i LUFS average, peaks no higher than $target_tp dBFS."
  gt "$peak_tolerance" 0 && \
    echo "Peaks may run up to $(f "$peak_tolerance" 1) dB over that ceiling to reach the loudness target."
  echo "Those two leave $(f "$budget" 1) dB between a track's average and its peaks."
  echo "A track whose peaks sit further above its average than that cannot have both,"
  echo "so it is turned down until the peaks fit and ends up quiet by the difference."
  echo
  hdr=$(printf "$hdr_fmt" track 'avg in' 'peak in' above gain 'avg out' tpeak speak quiet 'limited by')
  printf '%s\n' "$hdr"; log "$hdr"
fi

worst=0

shopt -s nullglob nocaseglob 2>/dev/null || true
files=$(ls -1 "$root"/*.wav "$root"/*.mp3 2>/dev/null | sort)
[ -n "$files" ] || { echo "no .wav or .mp3 files in $root" >&2; exit 1; }

while IFS= read -r src; do
  [ -n "$src" ] || continue
  total=$((total + 1))
  base=$(basename "$src"); stem=${base%.*}
  out="$dst/$stem.wav"
  [ "$mode" = gain ] || echo "measuring $base"

  # ---- pass 1: measure the mono signal ---------------------------------------
  # astats rides in the same chain to get SAMPLE peak, which loudnorm does not
  # report. Sample peak is what governs clipping on the s16 write.
  p1=$(ffmpeg -hide_banner -nostdin -nostats -i "$src" \
       -af "${pre},astats=measure_perchannel=none:measure_overall=Peak_level,loudnorm=I=${target_i}:TP=${target_tp}:LRA=${target_lra}:print_format=json" \
       -f null - 2>&1)

  jget() { printf '%s' "$p1" | grep -o "\"$1\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -1 | sed 's/.*"\([^"]*\)"$/\1/'; }
  in_i=$(jget input_i)
  if [ -z "$in_i" ]; then
    echo "WARNING: no measurement for $base -- ffmpeg said:" >&2
    printf '%s\n' "$p1" | grep -v '^[[:space:]]*$' | tail -6 | sed 's/^/    /' >&2
    log "  SKIP  $base  (no measurement)"
    skipped=$((skipped + 1))
    continue
  fi
  in_tp=$(jget input_tp); in_lra=$(jget input_lra)
  in_thresh=$(jget input_thresh); target_offset=$(jget target_offset)

  # Sample peak from astats. If it cannot be read the clamp is skipped for this
  # file and the post-write check is the only guard, so say so loudly.
  in_sp=$(printf '%s' "$p1" | grep -o 'Peak level dB:[[:space:]]*-\{0,1\}[0-9.]*\|Peak level dB:[[:space:]]*-\{0,1\}inf' \
          | head -1 | sed 's/.*:[[:space:]]*//')
  have_sp=1
  case "$in_sp" in ''|*inf*) have_sp=0; in_sp=0.0;
    warn "    ^ $stem: no sample-peak measurement, clip clamp disabled for this file" ;;
  esac

  flag=''; sample_peak=''; bound=''; gain=''; out_i=''; out_tp=''; out_lra=''
  quieter=''; lra_removed=''; type=''; plr=''

  if [ "$mode" = gain ]; then
    # ---- pass 2: one static gain, clamped by three ceilings ------------------
    gain_i=$(calc "$target_i" "$in_i" 0 'a-b')
    gain_tp=$(calc "$target_tp" "$peak_tolerance" "$in_tp" '(a+b)-c')
    if [ "$have_sp" = 1 ]; then
      gain_sp=$(calc "$sample_ceiling" "$in_sp" 0 'a-b')
    else
      gain_sp=999999
    fi
    gain=$(min3 "$gain_i" "$gain_tp" "$gain_sp")

    bound=$(awk -v g="$gain" -v gi="$gain_i" -v gs="$gain_sp" \
      'BEGIN{ if(g==gi) print "loudness"; else if(g==gs) print "sample peak"; else print "true peak" }')

    out_i=$(calc "$in_i" "$gain" 0 'a+b')
    out_tp=$(calc "$in_tp" "$gain" 0 'a+b')
    out_sp=$(calc "$in_sp" "$gain" 0 'a+b')
    short=$(calc "$target_i" "$out_i" 0 'a-b')     # dB quieter than the set, >= 0
    plr=$(calc "$in_tp" "$in_i" 0 'a-b')
    out_lra=$in_lra                                 # static gain does not change range
    lra_removed=0.00; type=static

    ffmpeg -hide_banner -nostdin -nostats -loglevel error -y -i "$src" \
           -af "${pre},volume=$(f "$gain")dB" \
           -ar 44100 -ac 2 -c:a pcm_s16le -dither_method triangular "$out" >/dev/null 2>&1

    quieter=$(f "$short")
    if gt "$short" 0.05; then short_txt=$(printf '%5s' "$(f "$short" 1)"); else short_txt='    -'; fi
    [ "$have_sp" = 1 ] && sp_txt=$(f "$out_sp" 1) || sp_txt='-'
    line=$(printf "$hdr_fmt" "$stem" "$(f "$in_i" 1)" "$(f "$in_tp" 1)" "$(f "$plr" 1)" \
           "$(f "$gain" 1)" "$(f "$out_i" 1)" "$(f "$out_tp" 1)" "$sp_txt" "$short_txt" "$bound")
    printf '%s\n' "$line"; log "$line"

    gt "$short" "$worst" && worst=$short

    if gt "$short" "$undershoot_warn"; then
      flag=QUIET
      warn "    ^ $stem plays $(f "$short" 1) dB quieter than the rest of the set. Its peaks sit $(f "$plr" 1) dB above its average, and only $(f "$budget" 1) dB fits between the two targets. It cannot be raised in the playlist without clipping - to match it, lower the rest."
    fi

    # Verify the clamp held, by re-measuring the file that was actually written.
    # With the sample-peak clamp in place it should never reach full scale; if it
    # does, the clamp failed and the file is damaged. Only run where it can
    # matter - a file written well under the ceilings has nothing to check.
    over_tp=$(calc "$target_tp" 0 0 'a+0.005')
    near_sp=$(calc "$sample_ceiling" 0 0 'a-0.5')
    if [ "$have_sp" = 0 ] || gt "$out_tp" "$over_tp" || gt "$out_sp" "$near_sp"; then
      sp_out=$(ffmpeg -hide_banner -nostdin -nostats -i "$out" \
               -af "${pre},astats=measure_perchannel=none:measure_overall=Peak_level" \
               -f null - 2>&1 | grep -o 'Peak level dB:[[:space:]]*-\{0,1\}[0-9.]*\|Peak level dB:[[:space:]]*-\{0,1\}inf' \
               | head -1 | sed 's/.*:[[:space:]]*//')
      sample_peak=${sp_out:-'?'}

      # An unreadable measurement is a FAILED check, not a passed one.
      if [ "$sample_peak" = '?' ]; then
        flag=UNVERIFIED; clipped=$((clipped + 1))
        warn "    ^ $stem: could not measure sample peak of the written file. Clip check did not run - treat as failed."
      elif case "$sample_peak" in *inf*) false ;; *) gt "$sample_peak" -0.05001 ;; esac; then
        flag=CLIPPED; clipped=$((clipped + 1))
        warn "    ^ $stem SAMPLE PEAK $sample_peak dBFS - samples were clipped on write. The clamp failed; do not use this file."
      elif gt "$out_tp" "$over_tp"; then
        [ -n "$flag" ] || flag=OVER
        log "    ^ $stem true peak $(f "$out_tp" 1) dBFS, $(f "$(calc "$out_tp" "$target_tp" 0 'a-b')" 1) dB over the ceiling, to reach $target_i LUFS. Sample peak $sample_peak dBFS - nothing clipped."
      fi
    fi
  else
    # ---- pass 2: original loudnorm, with the compression actually measured ----
    p2=$(ffmpeg -hide_banner -nostdin -nostats -y -i "$src" \
         -af "${pre},loudnorm=I=${target_i}:TP=${target_tp}:LRA=${target_lra}:measured_I=${in_i}:measured_TP=${in_tp}:measured_LRA=${in_lra}:measured_thresh=${in_thresh}:offset=${target_offset}:linear=true:print_format=json" \
         -ar 44100 -ac 2 -c:a pcm_s16le -dither_method triangular "$out" 2>&1)
    oget() { printf '%s' "$p2" | grep -o "\"$1\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | tail -1 | sed 's/.*"\([^"]*\)"$/\1/'; }
    out_i=$(oget output_i)
    if [ -n "$out_i" ]; then
      out_tp=$(oget output_tp); out_lra=$(oget output_lra); type=$(oget normalization_type)
      lra_removed=$(calc "$in_lra" "$out_lra" 0 'a-b')
      gain=$(calc "$out_i" "$in_i" 0 'a-b')
      plr=$(calc "$in_tp" "$in_i" 0 'a-b')
      quieter=$(calc "$target_i" "$out_i" 0 'a-b')
      line=$(printf '  %-34s in %7s LUFS / TP %6s / LRA %5s  -> %7s / %6s / LRA %5s  removed %5s LU  [%s]' \
             "$stem" "$(f "$in_i")" "$(f "$in_tp")" "$(f "$in_lra")" \
             "$(f "$out_i")" "$(f "$out_tp")" "$(f "$out_lra")" "$(f "$lra_removed")" "$type")
      printf '%s\n' "$line"; log "$line"
      gt "$lra_removed" "$worst" && worst=$lra_removed
      if gt "$lra_removed" "$lra_warn"; then
        flag=SQUASHED
        warn "    SQUASHED: $stem lost $(f "$lra_removed") LU of range"
      fi
    else
      type='?'
      warn "  no pass-2 report for $base"
    fi
  fi

  # Format check runs ALWAYS, not just under --verify. It is cheap (ffprobe reads
  # the header only) and it is the one failure that silently ruins playback.
  # sample_fmt, not bits_per_raw_sample: ffprobe reports the latter as N/A for
  # PCM streams, so that check can never match. Read by key, not position:
  # ffprobe emits fields in stream-struct order, not the order requested.
  pr=$(ffprobe -v error -show_entries stream=codec_name,channels,sample_rate,sample_fmt \
       -of default=noprint_wrappers=1 "$out" 2>&1)
  pv() { printf '%s' "$pr" | grep "^$1=" | head -1 | cut -d= -f2 | tr -d '[:space:]'; }
  probe="$(pv codec_name),$(pv channels),$(pv sample_rate),$(pv sample_fmt)"
  if [ "$probe" != "pcm_s16le,2,44100,s16" ]; then
    flag=BADFORMAT; bad_format=$((bad_format + 1))
    warn "    ^ $stem wrote as '$probe', expected 'pcm_s16le,2,44100,s16' - will NOT play correctly on a MUR"
  fi

  if [ "$verify" = 1 ]; then
    chk=$(ffmpeg -hide_banner -nostdin -nostats -i "$out" -af "${pre},ebur128=peak=true" -f null - 2>&1)
    vi=$(printf '%s' "$chk" | grep -o 'I:[[:space:]]*-\{0,1\}[0-9.]*[[:space:]]*LUFS' | tail -1 | sed 's/I:[[:space:]]*//; s/[[:space:]]*LUFS//')
    vp=$(printf '%s' "$chk" | grep -o 'Peak:[[:space:]]*-\{0,1\}[0-9.]*[[:space:]]*dBFS' | tail -1 | sed 's/Peak:[[:space:]]*//; s/[[:space:]]*dBFS//')
    v="    verify: $probe  mono I=${vi:-?} LUFS  peak=${vp:-?} dBFS"
    printf '%s\n' "$v"; log "$v"
  fi

  written=$((written + 1))
  csv "$base,$(f "$in_i"),$(f "$in_tp"),$(f "$in_sp"),$(f "$in_lra"),$([ -n "$plr" ] && f "$plr"),$(f "$budget"),$([ -n "$gain" ] && f "$gain"),$bound,$([ -n "$out_i" ] && f "$out_i"),$([ -n "$out_tp" ] && f "$out_tp"),$([ -n "$out_lra" ] && f "$out_lra"),$sample_peak,$([ -n "$quieter" ] && f "$quieter"),$([ -n "$lra_removed" ] && f "$lra_removed"),$type,$probe,$flag"
done <<EOF
$files
EOF

# ---- format gate ------------------------------------------------------------
echo
if [ "$bad_format" -eq 0 ] && [ "$skipped" -eq 0 ] && [ "$clipped" -eq 0 ]; then
  fmt="format: all $written files are pcm_s16le / 2 ch / 44100 Hz / s16, nothing clipped - OK for MUR"
else
  fmt="format: $bad_format of $written wrong format, $clipped clipped, $skipped input(s) produced no file - DO NOT UPLOAD"
fi
printf '%s\n' "$fmt"; log "$fmt"
if [ "$total" -ne "$written" ]; then
  warn "expected $total outputs, wrote $written"
fi

# ---- summary ----------------------------------------------------------------
echo
# grep -c prints its count AND exits 1 when that count is zero, so the obvious
# `|| echo 0` fallback appends a second zero and the summary reads "0\n0".
count_csv() { c=$(grep -c "$1" "$csv_path" 2>/dev/null) || true; printf '%s' "${c:-0}"; }

if [ "$mode" = gain ]; then
  quiet_n=$(count_csv ',QUIET$')
  s="done: $written files, static gain, nothing compressed. $quiet_n track(s) more than $(f "$undershoot_warn" 1) dB quiet; worst is $(f "$worst" 1) dB."
  if gt "$worst" 0.05; then
    s="$s
      To level the whole set to the quietest track, re-run with --target-i $(f "$(calc "$target_i" "$worst" 0 'a-b')" 1). Raising a quiet track at playback clips it."
  fi
else
  dyn=$(count_csv ',dynamic,')
  sq=$(count_csv ',SQUASHED$')
  s="done: $written files, $dyn dynamic. worst range loss $(f "$worst") LU, $sq flagged SQUASHED."
fi
printf '%s\n' "$s"; log "$s"
echo "log: $log_path"
echo "csv: $csv_path"

[ "$bad_format" -eq 0 ] && [ "$skipped" -eq 0 ] && [ "$clipped" -eq 0 ]
