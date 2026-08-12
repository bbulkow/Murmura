param(
  [ValidateSet('gain','loudnorm')]
  [string]$Mode = 'gain',      # 'gain' = static gain, never compresses (default)
                               # 'loudnorm' = original two-pass loudnorm, may compress
  [string]$Dest = 'norm2',
  [double]$TargetI = -14.0,
  [double]$TargetTP = -1.0,
  [double]$PeakTolerance = 1.5,  # dB the peak ceiling may be exceeded, when doing so
                                 # is what it takes to reach the loudness target.
                                 # 1.5 caps output true peak at +0.5 dBFS, which stays
                                 # under the sample-peak clip point for any normally
                                 # limited master. Set 0 for a hard -1.0 ceiling.
  [double]$SampleCeiling = -0.1,  # hard ceiling on SAMPLE peak in the written file.
                                  # This is the one that prevents clipping: s16 writes
                                  # truncate anything at or above 0 dBFS. Intersample
                                  # (true) peaks may still exceed it, per PeakTolerance.
  [double]$TargetLRA = 11.0,   # loudnorm mode only; ignored in gain mode
  [double]$UndershootWarn = 1.0,  # LU below target before a track is called out
  [double]$LraWarn = 1.0,         # LU of range removed before a track is called out
  [switch]$Verify              # re-measure each written file
)

# ---------------------------------------------------------------------------
# gain mode (default)
#
#   Pass 1 measures the mono signal. Pass 2 applies ONE static gain, the
#   smallest of three:
#
#       gain = min( TargetI - measured_I ,                    loudness target
#                   (TargetTP + PeakTolerance) - measured_TP , intersample
#                   SampleCeiling - measured_SP )              hard clip guard
#
#   The first reaches the loudness target. The second holds true (intersample)
#   peaks to the ceiling PLUS its tolerance - note it does not hold them to
#   TargetTP itself. The third is absolute: an s16 write truncates anything at
#   or above 0 dBFS, and true peak does not predict that, because on a master
#   limited flat to full scale sample peak and true peak differ by several dB.
#
#   The smallest wins, so a track lands BELOW the loudness target when a peak
#   ceiling says it must. That is why loudnorm is not used here: its second pass
#   refuses to undershoot, so when a flat gain would breach the ceiling it
#   switches to dynamic mode and rides the material instead. Nearly every
#   commercially mastered source peaks close to 0 dBFS, so nearly every file
#   took that path. Undershooting is the cheaper trade: no compressor touches
#   the audio, and the set can still be levelled - downward.
#
#   A track that undershoots by more than -UndershootWarn LU is flagged QUIET.
#   It CANNOT be raised at playback: MUR track volume is 0-100 mapped to
#   20*log10(v/100) (main/murmura.c), so 100 is unity and there is no gain above
#   it - and every file already sits against the peak ceiling anyway. To match
#   the set, lower the rest: re-run with -TargetI reduced by the worst
#   shortfall, which the summary prints for you.
#
# loudnorm mode
#
#   The original two-pass loudnorm. Kept for comparison. Reports the input->output
#   LRA delta, which is how much dynamic range the compressor actually removed,
#   rather than just reporting that it ran.
#
# Channel handling is unchanged from norm.ps1: sum to mono in the filter chain,
# duplicate to stereo with -ac 2. A MUR drives one speaker but its player reads
# mono files as interleaved stereo frames and plays them at 2x speed.
# ---------------------------------------------------------------------------

$inv  = [Globalization.CultureInfo]::InvariantCulture
function D([string]$s) { [double]::Parse($s, $inv) }
function F([double]$d, [int]$n = 2) { $d.ToString("F$n", $inv) }

$root = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$dst  = Join-Path $root $Dest
New-Item -ItemType Directory -Force -Path $dst | Out-Null

$stamp   = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
$logPath = Join-Path $dst 'normalize.log'
$csvPath = Join-Path $dst 'normalize.csv'

$pre = "aformat=channel_layouts=mono"
$rows = @()
$badFormat = 0
$skipped = 0
$clipped = 0

# The two targets together allow a fixed distance between a track's average
# level and its peaks. A track whose peaks sit further above its average than
# this cannot satisfy both, and lands quiet by the difference.
$budget = ($TargetTP + $PeakTolerance) - $TargetI

function Log([string]$line) {
  Add-Content -Path $logPath -Value $line -Encoding UTF8
}

Log ""
Log "=== $stamp  mode=$Mode  I=$TargetI TP=$TargetTP LRA=$TargetLRA  -> $Dest ==="

$files = Get-ChildItem -Path (Join-Path $root '*') -Include *.wav,*.mp3 -File | Sort-Object Name

if ($Mode -eq 'gain') {
  Write-Host ""
  Write-Host "Targets: $TargetI LUFS average, peaks no higher than $TargetTP dBFS."
  if ($PeakTolerance -gt 0) {
    Write-Host "Peaks may run up to $(F $PeakTolerance 1) dB over that ceiling to reach the loudness target."
  }
  Write-Host "Those two leave $(F $budget 1) dB between a track's average and its peaks."
  Write-Host "A track whose peaks sit further above its average than that cannot have both,"
  Write-Host "so it is turned down until the peaks fit and ends up quiet by the difference."
  Write-Host ""
  $hdr = "  {0,-30} {1,7} {2,7}   {3,5}    {4,6}   {5,7} {6,6} {7,6}   {8}  {9}" -f `
         'track', 'avg in', 'peak in', 'above', 'gain', 'avg out', 'tpeak', 'speak', 'quiet', 'limited by'
  Write-Host $hdr
  Log $hdr
}

foreach ($f in $files) {
  $src = $f.FullName
  $out = Join-Path $dst "$($f.BaseName).wav"
  if ($Mode -ne 'gain') { Write-Host "measuring $($f.Name)" }

  # ---- pass 1: measure the mono signal -------------------------------------
  # astats rides in the same chain to get SAMPLE peak, which loudnorm does not
  # report. Sample peak is what governs clipping on the s16 write; loudnorm's
  # input_tp is TRUE peak, an oversampled reconstruction figure, and on a master
  # limited flat to full scale the two differ by several dB.
  $p1 = ffmpeg -hide_banner -nostdin -nostats -i $src `
        -af "${pre},astats=measure_perchannel=none:measure_overall=Peak_level,loudnorm=I=${TargetI}:TP=${TargetTP}:LRA=${TargetLRA}:print_format=json" `
        -f null - 2>&1 | Out-String
  $json = [regex]::Match($p1, '(?s)\{[^{}]*"input_i".*?\}').Value
  if (-not $json) {
    Write-Warning "no measurement for $($f.Name) -- ffmpeg said:"
    ($p1 -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 6) |
      ForEach-Object { Write-Host "    $_" }
    Log "  SKIP  $($f.Name)  (no measurement)"
    $skipped++
    continue
  }
  $m = $json | ConvertFrom-Json

  $inI   = D $m.input_i
  $inTP  = D $m.input_tp
  $inLRA = D $m.input_lra

  # Sample peak from astats. If it cannot be parsed the clamp is skipped for this
  # file and the post-write check is the only guard, so say so loudly.
  $spm1  = [regex]::Match($p1, 'Peak level dB:\s*(-?[\d.]+|-?inf)')
  $haveSP = $spm1.Success -and $spm1.Groups[1].Value -notmatch 'inf'
  $inSP  = if ($haveSP) { D $spm1.Groups[1].Value } else { 0.0 }
  if (-not $haveSP) {
    $w = "    ^ $($f.BaseName): no sample-peak measurement, clip clamp disabled for this file"
    Write-Warning $w; Log $w
  }

  $row = [ordered]@{
    file = $f.Name; in_i = F $inI; in_tp = F $inTP; in_sp = F $inSP; in_lra = F $inLRA
    plr = ''; budget = ''
    gain_db = ''; bound_by = ''; out_i = ''; out_tp = ''; out_lra = ''; sample_peak = ''
    quieter_by_db = ''; lra_removed_lu = ''; type = ''; format = ''; flag = ''
  }

  if ($Mode -eq 'gain') {
    # ---- pass 2: one static gain, clamped by three ceilings ----------------
    #   gainI  - reach the loudness target
    #   gainTP - keep true peak within the ceiling plus its tolerance
    #   gainSP - keep SAMPLE peak below full scale. This one is not negotiable:
    #            exceeding it means ffmpeg truncates samples writing s16.
    $gainI  = $TargetI  - $inI
    $gainTP = ($TargetTP + $PeakTolerance) - $inTP
    $gainSP = if ($haveSP) { $SampleCeiling - $inSP } else { [double]::MaxValue }
    $gain   = [math]::Min([math]::Min($gainI, $gainTP), $gainSP)

    $bound = if ($gain -eq $gainI) { 'loudness' }
             elseif ($gain -eq $gainSP) { 'sample peak' }
             else { 'true peak' }

    $outI  = $inI  + $gain
    $outTP = $inTP + $gain
    $outSP = $inSP + $gain
    $short = $TargetI - $outI          # dB quieter than the rest of the set, >= 0

    # How far this track's peaks sit above its own average level, and how far
    # apart the two targets are. Excess of the first over the second IS $short.
    $plr = $inTP - $inI

    $filt = "${pre},volume=$(F $gain)dB"
    ffmpeg -hide_banner -nostdin -nostats -loglevel error -y -i $src -af $filt `
           -ar 44100 -ac 2 -c:a pcm_s16le -dither_method triangular $out 2>&1 | Out-Null

    $row.plr = F $plr; $row.budget = F $budget
    $row.gain_db = F $gain; $row.bound_by = $bound
    $row.out_i = F $outI; $row.out_tp = F $outTP
    $row.out_lra = F $inLRA            # static gain does not change range
    $row.quieter_by_db = F $short; $row.lra_removed_lu = '0.00'; $row.type = 'static'

    $shortTxt = if ($short -gt 0.05) { "{0,5}" -f (F $short 1) } else { "    -" }
    $line = "  {0,-30} {1,7} {2,7}   {3,5}    {4,6}   {5,7} {6,6} {7,6}   {8}  {9}" -f `
            $f.BaseName, (F $inI 1), (F $inTP 1), (F $plr 1), (F $gain 1), `
            (F $outI 1), (F $outTP 1), (F $outSP 1), $shortTxt, $bound
    Write-Host $line
    Log $line

    if ($short -gt $UndershootWarn) {
      $row.flag = 'QUIET'
      $w = "    ^ $($f.BaseName) plays $(F $short 1) dB quieter than the rest of the set. Its peaks sit $(F $plr 1) dB above its average, and only $(F $budget 1) dB fits between the two targets. It cannot be raised in the playlist without clipping - to match it, lower the rest."
      Write-Warning $w; Log $w
    }

    # Verify the clamp held. This re-measures the SAMPLE peak of the file that was
    # actually written; with the gainSP clamp in place it should never reach full
    # scale. If it does, the clamp failed and the file is damaged - not a warning
    # to live with. Only run where it can matter: a file written well under the
    # ceilings has nothing to check.
    if (-not $haveSP -or $outTP -gt ($TargetTP + 0.005) -or $outSP -gt ($SampleCeiling - 0.5)) {
      $sp = ffmpeg -hide_banner -nostdin -nostats -i $out `
            -af "${pre},astats=measure_perchannel=none:measure_overall=Peak_level" `
            -f null - 2>&1 | Out-String
      $spm = [regex]::Match($sp, 'Peak level dB:\s*(-?[\d.]+|-?inf)')
      $spv = if ($spm.Success) { $spm.Groups[1].Value } else { '?' }
      $row.sample_peak = $spv

      # An unreadable measurement is a FAILED check, not a passed one.
      if ($spv -eq '?') {
        $row.flag = 'UNVERIFIED'
        $clipped++
        $w = "    ^ $($f.BaseName): could not measure sample peak of the written file. Clip check did not run - treat as failed."
        Write-Warning $w; Log $w
      }
      elseif ($spv -notmatch 'inf' -and (D $spv) -ge -0.05) {
        $row.flag = 'CLIPPED'
        $clipped++
        $w = "    ^ $($f.BaseName) SAMPLE PEAK $spv dBFS - samples were clipped on write. The clamp failed; do not use this file."
        Write-Warning $w; Log $w
      }
      elseif ($outTP -gt ($TargetTP + 0.005)) {
        if (-not $row.flag) { $row.flag = 'OVER' }
        Log ("    ^ $($f.BaseName) true peak $(F $outTP 1) dBFS, $(F ($outTP - $TargetTP) 1) dB over the ceiling, to reach $TargetI LUFS. Sample peak $spv dBFS - nothing clipped.")
      }
    }
  }
  else {
    # ---- pass 2: original loudnorm, with the compression actually measured --
    $filt = "${pre},loudnorm=I=${TargetI}:TP=${TargetTP}:LRA=${TargetLRA}" +
            ":measured_I=$($m.input_i):measured_TP=$($m.input_tp)" +
            ":measured_LRA=$($m.input_lra):measured_thresh=$($m.input_thresh)" +
            ":offset=$($m.target_offset):linear=true:print_format=json"
    $p2 = ffmpeg -hide_banner -nostdin -nostats -y -i $src -af $filt `
                 -ar 44100 -ac 2 -c:a pcm_s16le -dither_method triangular $out 2>&1 | Out-String
    $j2 = [regex]::Match($p2, '(?s)\{[^{}]*"input_i".*?\}').Value

    if ($j2) {
      $o = $j2 | ConvertFrom-Json
      $outI = D $o.output_i; $outTP = D $o.output_tp; $outLRA = D $o.output_lra
      $removed = $inLRA - $outLRA
      $row.out_i = F $outI; $row.out_tp = F $outTP; $row.out_lra = F $outLRA
      $row.gain_db = F ($outI - $inI)
      $row.plr = F ($inTP - $inI); $row.budget = F $budget
      $row.quieter_by_db = F ($TargetI - $outI)
      $row.lra_removed_lu = F $removed
      $row.type = $o.normalization_type

      $line = "  {0,-34} in {1,7} LUFS / TP {2,6} / LRA {3,5}  -> {4,7} / {5,6} / LRA {6,5}  removed {7,5} LU  [{8}]" -f `
              $f.BaseName, (F $inI), (F $inTP), (F $inLRA), (F $outI), (F $outTP), (F $outLRA), (F $removed), $o.normalization_type
      Write-Host $line
      Log $line

      if ($removed -gt $LraWarn) {
        $row.flag = 'SQUASHED'
        $w = "    SQUASHED: $($f.BaseName) lost $(F $removed) LU of range"
        Write-Warning $w; Log $w
      }
    } else {
      $row.type = '?'
      Write-Warning "  no pass-2 report for $($f.Name)"
      Log "  $($f.BaseName)  no pass-2 report"
    }
  }

  # Format check runs ALWAYS, not just under -Verify. It is cheap (ffprobe reads
  # the header only) and it is the one failure that silently ruins playback.
  # sample_fmt, not bits_per_raw_sample: ffprobe reports the latter as N/A for
  # PCM streams, so the README's suggested check never matches.
  # Parse key=value: ffprobe emits fields in stream-struct order, NOT the order
  # they were requested in, so positional comparison is not safe.
  $pr = ffprobe -v error -show_entries stream=codec_name,channels,sample_rate,sample_fmt `
                -of default=noprint_wrappers=1 $out 2>&1 | Out-String
  $pv = @{}
  foreach ($kv in ($pr -split "`r?`n")) {
    if ($kv -match '^\s*([a-z_]+)=(.*)$') { $pv[$Matches[1]] = $Matches[2].Trim() }
  }
  $probe = "{0},{1},{2},{3}" -f $pv['codec_name'], $pv['channels'], $pv['sample_rate'], $pv['sample_fmt']
  if ($pv['codec_name'] -ne 'pcm_s16le' -or $pv['channels'] -ne '2' -or
      $pv['sample_rate'] -ne '44100'   -or $pv['sample_fmt'] -ne 's16') {
    $row.flag = 'BADFORMAT'
    $badFormat++
    $w = "    ^ $($f.BaseName) wrote as '$probe', expected 'pcm_s16le,2,44100,s16' - will NOT play correctly on a MUR"
    Write-Warning $w; Log $w
  }
  $row.format = $probe

  if ($Verify) {
    $chk = ffmpeg -hide_banner -nostdin -nostats -i $out `
           -af "${pre},ebur128=peak=true" -f null - 2>&1 | Out-String
    $mi = [regex]::Matches($chk, 'I:\s*(-?[\d.]+)\s*LUFS')    | Select-Object -Last 1
    $mp = [regex]::Matches($chk, 'Peak:\s*(-?[\d.]+)\s*dBFS') | Select-Object -Last 1
    $vi = if ($mi) { $mi.Groups[1].Value } else { '?' }
    $vp = if ($mp) { $mp.Groups[1].Value } else { '?' }
    $v = "    verify: $probe  mono I=$vi LUFS  peak=$vp dBFS"
    Write-Host $v
    Log $v
  }

  $rows += [pscustomobject]$row
}

$rows | Export-Csv -Path $csvPath -NoTypeInformation -Encoding UTF8

# ---- format gate ----------------------------------------------------------
Write-Host ""
$fmt = if ($badFormat -eq 0 -and $skipped -eq 0 -and $clipped -eq 0) {
  "format: all $($rows.Count) files are pcm_s16le / 2 ch / 44100 Hz / s16, nothing clipped - OK for MUR"
} else {
  "format: $badFormat of $($rows.Count) wrong format, $clipped clipped, $skipped input(s) produced no file - DO NOT UPLOAD"
}
Write-Host $fmt
Log $fmt
if ($files.Count -ne $rows.Count) {
  $w = "expected $($files.Count) outputs, wrote $($rows.Count)"
  Write-Warning $w; Log $w
}

# ---- summary --------------------------------------------------------------
Write-Host ""
if ($Mode -eq 'gain') {
  $u = $rows | Where-Object { $_.quieter_by_db } | ForEach-Object { D $_.quieter_by_db }
  $quiet = ($rows | Where-Object { $_.flag -eq 'QUIET' }).Count
  $worst = if ($u) { ($u | Measure-Object -Maximum).Maximum } else { 0 }
  $s = "done: {0} files, static gain, nothing compressed. {1} track(s) more than {2} dB quiet; worst is {3} dB." -f `
       $rows.Count, $quiet, (F $UndershootWarn 1), (F $worst 1)
  if ($worst -gt 0.05) {
    $s += "`n      To level the whole set to the quietest track, re-run with -TargetI $(F ($TargetI - $worst) 1). Raising a quiet track at playback clips it."
  }
} else {
  $dyn = ($rows | Where-Object { $_.type -eq 'dynamic' }).Count
  $r = $rows | Where-Object { $_.lra_removed_lu } | ForEach-Object { D $_.lra_removed_lu }
  $worst = if ($r) { ($r | Measure-Object -Maximum).Maximum } else { 0 }
  $s = "done: {0} files, {1} dynamic. worst range loss {2} LU, {3} flagged SQUASHED." -f `
       $rows.Count, $dyn, (F $worst), ($rows | Where-Object { $_.flag -eq 'SQUASHED' }).Count
}
Write-Host $s
Log $s
Write-Host "log: $logPath"
Write-Host "csv: $csvPath"

# Exit non-zero when the gate said DO NOT UPLOAD, so a caller can act on it
# rather than having to scrape the text. Mirrors normalize.sh.
if ($badFormat -ne 0 -or $skipped -ne 0 -or $clipped -ne 0) { exit 1 }
