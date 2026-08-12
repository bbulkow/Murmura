# Mastering audio for MURs

Every file a MUR plays must be **44.1 kHz, 16-bit, stereo PCM WAV**, level-matched
to **-14 LUFS** measured on the mono sum. Level-matching is a single static gain,
clamped so nothing clips.

Use these exact numbers. The point is that every track plays back at the same
level, so one file mastered hotter than the rest undoes the work on all of them.
Source format does not matter — MP3, M4A, FLAC and WAV all work, and a source
peaking above 0 dBFS is expected and needs no correction beforehand.

`normalize.ps1` (Windows) and `normalize.sh` (macOS/Linux) at the repo root do all
of it. They take the same options, produce the same output, and both exit non-zero
when the result is not safe to upload. Working by hand: [Appendix A](#appendix-a--by-hand).

## Targets

| | |
|---|---|
| Container / codec | WAV, `pcm_s16le` |
| Sample rate | 44100 Hz |
| Channels | 2, both carrying the same signal |
| Integrated loudness | -14 LUFS, measured on the mono sum |
| True peak | -1.0 dBFS, may reach +0.5 dBFS where that is what it takes to hit -14 |
| Sample peak | -0.1 dBFS, never exceeded |

The format half of this is a hard requirement of the firmware and is stated in
[AGENTS.md](AGENTS.md#audio-file-format-hard-requirement); the analysis of why a
mono file plays at exactly double speed, and the three ADF-supported ways it could
be fixed, is in [main/README.md](main/README.md). This document is about the
loudness half.

## Sum to mono, then duplicate to stereo

A MUR drives one speaker, so a true stereo file would send half its content
nowhere. But a mono *file* plays at double speed, because the mixer is pinned to
stereo and reads a mono byte stream as interleaved stereo frames. Both channels
must therefore carry the same summed signal:

```
aformat=channel_layouts=mono   sums to mono inside the filter chain
-ac 2                          duplicates that mono into both output channels
```

The two settings are deliberately different. Do not "fix" this to match.
Duplicating changes file size, not duration, so playlist durations stay valid.

**Measure in mono for the same reason.** Mono is the signal that reaches the
speaker, so -14 LUFS measured there is -14 LUFS heard. The finished dual-mono file
*measures* about 3 LU hotter, near -11 LUFS, because EBU R128 sums both channels.
That is expected. Do not correct it.

## One static gain, not `loudnorm`'s second pass

The gain applied to the whole file is the smallest of three:

```
gain = min( -14.0 - integrated_loudness ,     reach the loudness target
             +0.5 - true_peak ,               intersample ceiling + tolerance
             -0.1 - sample_peak )             hard clip guard
```

The first reaches the target. The second holds intersample peaks to the ceiling
plus its tolerance — note it does not hold them to -1.0 itself. The third is
absolute: a 16-bit write truncates anything at or above 0 dBFS, and **true peak
does not predict that**. On a master limited flat to full scale the two differ by
several decibels, which is why a true-peak ceiling alone never actually prevented
clipping.

`loudnorm`'s two-pass form is not used, because it will not undershoot the loudness
target. When the flat gain needed to reach -14 would breach the peak ceiling,
`loudnorm` reverts to dynamic mode and compresses the material to hit the number.
Commercially mastered sources are almost universally limited close to 0 dBFS, so
that is the normal case, not the exception. Undershooting is the cheaper trade: no
compressor touches the audio, and the set can still be levelled — downward.

## Tracks whose peaks sit far above their average land quiet

The targets allow a fixed distance between a track's average level and its peaks:
**14.5 dB**, from -14 LUFS up to the +0.5 dBFS tolerance. A track more dynamic than
that cannot satisfy both constraints, so it is turned down until the peaks fit and
ends up below -14 by the difference. The scripts flag these `QUIET` and print the
shortfall and which of the three clamps bound it.

**Do not try to raise a quiet track at playback.** Two independent reasons:

- Every file already sits against the peak ceiling, so gain above unity clips it.
- There is no gain above unity to apply. MUR track volume is 0-100, mapped in
  [main/murmura.c](main/murmura.c#L694) as `gain_db = 20*log10(volume/100)` and
  clamped to 100 — so 100 *is* unity and it is the maximum. A playlist entry's
  `volume` (see [ENSEMBLES.md](ENSEMBLES.md)) can only attenuate, and so can
  `device_volume` and `global_volume`.

The level is only recoverable downward: lower everything else. Re-run the whole set
with the loudness target reduced by the largest shortfall — `-TargetI` / `--target-i`,
and the summary prints the exact value to use — and every track then reaches it.
This is exact rather than approximate: dropping the target by the worst shortfall
shifts the first clamp by that amount for every track while the two peak clamps stay
fixed, so each track lands on the new target. The whole set gets quieter by that
amount, which is the price of matching.

The alternative is to accept the difference and leave it alone. Do not compress a
track to close the gap.

## Running it

```powershell
.\normalize.ps1                      # Windows
```
```bash
./normalize.sh                       # macOS / Linux
```

Both read every `.wav` and `.mp3` beside the script and write results to `norm2/`,
along with `normalize.log` and a `normalize.csv` carrying every measurement.

| option (ps1 / sh) | default | |
|---|---|---|
| `-Mode` / `--mode` | `gain` | `loudnorm` re-runs the old two-pass form for comparison |
| `-Dest` / `--dest` | `norm2` | output directory |
| `-TargetI` / `--target-i` | `-14.0` | `loudnorm` only accepts -70 to -5 |
| `-TargetTP` / `--target-tp` | `-1.0` | |
| `-PeakTolerance` / `--peak-tolerance` | `1.5` | set 0 for a hard -1.0 ceiling |
| `-SampleCeiling` / `--sample-ceiling` | `-0.1` | the clip guard; do not raise |
| `-UndershootWarn` / `--undershoot-warn` | `1.0` | LU below target before `QUIET` |
| `-Verify` / `--verify` | off | re-measure every written file |

Output looks like this — `limited by` is the answer to "why is this one quiet":

```
  track            avg in peak in  above    gain  avg out  tpeak  speak  quiet  limited by
  a_loud            -22.2  -18.3     4.0    17.2     -5.0   -1.0   -1.0      -  loudness
  d_spiky           -28.1  -18.1    10.0    18.0    -10.1   -0.1   -0.1    5.1  sample peak
```

The run ends with a gate. **Read it — it is the only thing standing between a
wrong file and a silent playback failure**, and both scripts exit non-zero when it
fails, so it can be scripted:

```
format: all 2 files are pcm_s16le / 2 ch / 44100 Hz / s16, nothing clipped - OK for MUR
```

Files flagged `BADFORMAT`, `CLIPPED` or `UNVERIFIED` fail it. `UNVERIFIED` means the
written file's sample peak could not be measured — treated as a failure, not a pass,
because an unreadable check is not a passed check.

## Verifying a finished file

```
ffprobe -v error -show_entries stream=codec_name,channels,sample_rate,sample_fmt -of default=noprint_wrappers=1 output.wav
```

Must report `pcm_s16le`, `2`, `44100`, `s16`. Two traps here, both verified:

- **Read the fields by key, not position.** ffprobe emits them in stream-struct
  order, not the order requested — asking for `channels,sample_rate` returns
  `44100,2`.
- **`bits_per_raw_sample` is `N/A` for PCM streams** and cannot be used for the
  depth check. `sample_fmt` is the field that works.

```
ffmpeg -i output.wav -af aformat=channel_layouts=mono,astats=measure_perchannel=none:measure_overall=Peak_level,ebur128=peak=true -f null -
```

The last `I:` line should read about -14 LUFS — this measures the mono sum, so it
is the number you targeted, not the ~-11 the finished stereo file measures — and
`Peak level dB` must be below -0.1.

---

## Appendix A — by hand

Install ffmpeg with `brew install ffmpeg` (macOS) or `winget install Gyan.FFmpeg`
(Windows). The commands are identical on both.

**Measure.** Writes nothing; prints `Peak level dB` and a JSON block.

```
ffmpeg -i input.mp3 -af aformat=channel_layouts=mono,astats=measure_perchannel=none:measure_overall=Peak_level,loudnorm=I=-14:TP=-1.0:print_format=json -f null -
```

```
[Parsed_astats_1 @ ...] Peak level dB: -0.35
{
    "input_i" : "-9.40",
    "input_tp" : "0.09"
}
```

**Compute** the gain from those three numbers:

```
min( -14.0 - (-9.40) ,  0.5 - 0.09 ,  -0.1 - (-0.35) )
    = min( -4.60 ,  0.41 ,  0.25 )
    = -4.60 dB          <- loudness-bound, lands exactly on target
```

That is the ordinary case: a loud modern master, turned down onto the target, with
both peak clamps slack. Watch for the other one — a track whose peaks sit more than
14.5 dB above its average comes out peak-bound and lands short of -14. If the
smallest term is *not* the first, the file is `QUIET` by the difference; see
[above](#tracks-whose-peaks-sit-far-above-their-average-land-quiet) for what to do
about it, which is not "raise it at playback".

**Apply.**

```
ffmpeg -i input.mp3 -af aformat=channel_layouts=mono,volume=-4.60dB -ar 44100 -ac 2 -c:a pcm_s16le -dither_method triangular output.wav
```

`-dither_method triangular` covers the requantization the gain change forces.

For a batch, loop these three steps — or just use the scripts, which also run the
format gate and the clip check.

## Appendix B — superseded procedure

Earlier revisions specified **-12 LUFS** via two-pass `loudnorm` with `LRA=11`, and
a hard -1.0 dBFS true-peak ceiling with no sample-peak clamp. Files produced under
that spec are compressed and about 2 LU hot. Regenerate them from source.

An earlier revision also stated that mono was the player requirement. That was
wrong, and files produced under it play at exactly double speed.
