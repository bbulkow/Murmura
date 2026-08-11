# Firmware notes

## Audio file format: why mono files play at double speed

**Requirement: 44100 Hz, 16-bit, stereo PCM WAV.** See AGENTS.md for the
operator-facing version. This is the analysis, and what it would take to lift the
limitation.

Status: **not fixed, deliberately.** The files get converted instead. Three
ADF-supported fixes are described below; all were costed, none was applied.

**Read option C first.** B1 and B2 make stereo output tolerate mono files.
C questions whether the output should be stereo at all — which, given how these
speakers are actually deployed, is probably the real answer.

### The pipeline does not transcode

```
per track:  fatfs_stream --> wav_decoder --\
            fatfs_stream --> wav_decoder ---> downmix --> i2s --> DAC
            fatfs_stream --> wav_decoder --/
```

Three source pipelines feed one `downmix` element, whose output feeds one I2S
writer. There is no resampler and no format converter on this path
(`filter_resample.h` appears only in `murmura_passthrough.c`, which is not the
live pipeline).

### The bug

`downmix` is told its input format exactly once, at startup
(`murmura.c`, `audio_stream_init`):

```c
esp_downmix_input_info_t source_info[MAX_TRACKS];
for (int i = 0; i < MAX_TRACKS; i++) {
    source_info[i].samplerate = 44100;  // "Will be updated when track starts"
    source_info[i].channel    = 2;      // "Will be updated when track starts"
    source_info[i].bits_num   = 16;
    ...
}
source_info_init(stream->downmix_e, source_info);
```

**Those two comments are false.** `source_info_init()` is called only there. The
only per-track downmix call at runtime is `downmix_set_gain_info()`, which sets
gain, not format. The I2S clock is likewise pinned once:

```c
i2s_stream_set_clk(stream->i2s_e, 44100, 16, 2);
```

and nothing handles the decoder's `AEL_MSG_CMD_REPORT_MUSIC_INFO` — the event
block that would is inside a disabled `#if ... #endif // useful debug`.

So when a mono file is played, `downmix` reads its byte stream as interleaved
stereo frames. It consumes two mono samples per output frame, so the file drains
in half the time: **exactly 2x speed**. Nothing errors, because nothing compared
the file's header to what the mixer was told.

A knock-on effect: mur-conductor computes each playlist entry's span from the
WAV header, so a mono entry finishes in half its declared duration and the
conductor waits out the remainder in silence. Same root cause.

### The DAC is not the problem and must not be changed

The I2S output is stereo and stays stereo — there are three tracks mixed to one
output, so the output format is fixed by design. Any fix belongs on the **input**
side of the downmixer, or in the decoder's output. `output_type` stays
`ESP_DOWNMIX_OUTPUT_TYPE_TWO_CHANNEL`.

### Fix option B1: tell downmix the truth (recommended if this is revisited)

ADF already supports per-source, per-index format updates at runtime
(`components/esp-adf-libs/esp_codec/include/codec/downmix.h`):

```c
esp_err_t downmix_set_source_stream_info(audio_element_handle_t self,
                                         int rate, int ch, int index);
//  ch:    "Channel number of the input stream. Only supported mono and dual."
//  index: "The index of input stream."
```

And `downmix` is explicitly built to accept mono sources and emit stereo —
`esp_downmix.h`, on `ESP_DOWNMIX_OUT_CTX_NORMAL`:

> *"If all input streams are mono, per channel of output stream contain all
> content of all input streams."*

So the mixer will do the right thing as soon as it is told the right thing. The
canonical pattern is in ADF's own example,
`examples/audio_processing/pipeline_audio_forge/main/audio_forge_pipeline_main.c`:

```c
} else if (msg.cmd == AEL_MSG_CMD_REPORT_MUSIC_INFO) {
    audio_element_getinfo(wav_decoder[i], &music_info);
    /* relay the DECODED format to the mixer, per source index */
    audio_forge_set_src_info(audio_forge, src_info, i);
}
```

For our pipeline that becomes: on `AEL_MSG_CMD_REPORT_MUSIC_INFO` from track
*i*'s `decode_e`, call
`downmix_set_source_stream_info(downmix_e, info.sample_rates, info.channels, i)`.

Why this is the cheap option: it is per-index, so it cannot disturb the other two
tracks (unlike `source_info_init()`, which rewrites all sources at once); it adds
no elements, no RAM and no CPU; and it is the same call shape as the
`downmix_set_gain_info()` calls this code already makes per track at runtime.

Unresolved before implementing:

1. **Does `downmix` resample differing per-source sample rates?** It accepts a
   per-source `samplerate`, which implies yes, but this was not confirmed. If it
   does not, B1 fixes mono only and a 22050 Hz file still misplays.
2. **Timing.** The format must reach `downmix` before it consumes the first bytes
   of the new track. The music-info report fires just after header parse, so the
   window is small, but listen for a transient at track start.
3. **`bits` is not settable** through that function — only rate and channels.
   `bits_num` stays at whatever `source_info_init()` set. 16-bit remains a hard
   requirement either way.

### Fix option B2: an rsp_filter per track

Insert `filter_resample` between each decoder and the downmixer, configured
`dest_rate = 44100, dest_ch = 2`, in `RESAMPLE_DECODE_MODE`. Per
`filter_resample.h`:

> *"If the esp_resample_mode_t is `RESAMPLE_DECODE_MODE`, `src_rate` and `src_ch`
> will be fetched from `audio_element_getinfo`."*

It configures itself from the decoder, so no event handling is needed and
`downmix`'s hard-coded stereo assumption becomes *true* rather than a lie. This
also fixes wrong sample rates, not just mono, and `rsp_filter_change_src_info()`
exists if a source changes mid-run.

Cost: three more elements' RAM plus resampling CPU per active track, on a device
already tight on both (see ESP32_DMA_MEMORY_ANALYSIS.md). That is why B1 is
preferred if only mono needs solving.

### Fix option C: make the output mono — the actual footgun removal

**Stereo is the questionable assumption here, not mono.**

These are not left/right pairs. A MUR usually drives **one** speaker, and
spatialisation across an installation is done by *placing MURs in the room* and
trimming each one with `playback_offset_us` — that per-device knob exists
precisely because the stereo field is the room, not the device. So the output
being stereo buys nothing, while costing:

- **the whole class of bug above.** With mono output, channel count stops
  mattering: `downmix` accepts mono *and* dual sources (`downmix.h`: *"Only
  supported mono and dual"*) and emits one channel. No file layout can be
  silently wrong.
- **half the SD card, and half the read bandwidth.** Mono files are half the
  size. The 17-file playlist that motivated this is 350 MB mono and 730 MB
  stereo, and every byte is read off SD and pushed through a decoder and ring
  buffer per track, three tracks at once, on a device already tight on DMA-capable
  RAM (see `ESP32_DMA_MEMORY_ANALYSIS.md`).
- **the conversion step entirely.** Content arrives mono; today it must be
  doubled in size purely to satisfy the mixer.

ADF supports this directly:

```c
downmix_cfg.downmix_info.output_type = ESP_DOWNMIX_OUTPUT_TYPE_ONE_CHANNEL;  // = 1
downmix_cfg.downmix_info.out_ctx     = ESP_DOWNMIX_OUT_CTX_LEFT_RIGHT;       // = 0
...
audio_element_setinfo(i2s_e, &(audio_element_info_t){ .channels = 1, ... });
i2s_stream_set_clk(i2s_e, 44100, 16, 1);
```

Two details that matter:

- **`out_ctx` must change too.** It is currently `ESP_DOWNMIX_OUT_CTX_NORMAL`,
  which maps L to L and R to R. With a one-channel output that risks discarding a
  stereo source's right channel. `ESP_DOWNMIX_OUT_CTX_LEFT_RIGHT` is documented as
  *"Include left and right channel content of all input streams in per channel of
  output stream"* — i.e. the sum, which is what a single speaker wants.
- **ADF already handles the ESP32's mono I2S quirk.** `i2s_stream.c` `_i2s_write`
  calls `i2s_mono_fix(info.bits, buffer, len)` whenever `info.channels == 1`, so
  mono output is a supported configuration rather than something to hand-roll.

Risks to settle before doing it:

1. **Verify on hardware which physical output(s) the ES8388 drives** in mono. If
   only one speaker terminal carries audio, the wiring convention becomes
   load-bearing — a MUR wired to the "wrong" terminal goes silent. This is the
   one thing that genuinely needs a scope or an ear on each board, and the reason
   the DAC config was left alone for now.
2. **Summing can phase-cancel.** Stereo material with out-of-phase content
   partially disappears when summed. Check any existing stereo content by ear.
3. **A two-speaker-per-MUR install would lose L/R.** If that is ever wanted, this
   becomes a config field rather than a constant.
4. **Sample rate still matters.** Mono output does not fix a 22050 Hz file. The
   requirement shrinks from three constraints to two (44100 Hz, 16-bit) — pair
   this with B1 or B2 if arbitrary rates are ever needed.

### Guard worth adding either way

The config server already parses these WAV headers to fill playlist durations
(`_parse_wav_header` / `_probe_wav_duration` in `mur-config-server/app.py`, and
`GET /api/ensemble/<group>/probe`), so it knows the channel count, sample rate
and bit depth of every file it offers. It could flag a non-conforming file at
selection time instead of letting it be discovered by ear. Not implemented.
