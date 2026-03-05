# To make a program with ESP-ADF and the AIThinker

At the time, in 2024, the AI THINKER esp32 boards were readily available and pretty excellent.
They had built-in SD card, they had a nice audio output, they had pins exposed.

However, it seems that between being hard to use, and being now very large,
they are not available. Therefore these instructions can really only be used
with the 40 AI THINKERS brian has in stock, and a new hardware platform
will be needed later.

As of 2026, here are some options.

The Espressif LyraT Mini is in stock and produced by Espressif at about $20 on Digi.

The Waveshare ESP32-S3-Audio board is about $15 and very similar to the AITHINKER

There is a SONATINO. It's out of stock because the guy who designed it lost
interest. However, one can grab his files - he posted them - update the audio
chip to something in stock - and get some made.

## Get esp-adf

Use esp-adf version, I used branch 2.7 (because it'll be well known instead of master).
That tag was last created and labeled Sept 2024.

It appears there are no updated tags in esp-adf, but there are code checkins.
Esp-adf has support for esp-idf v5.5 now, for example.

```
git clone -b v2.7 --single-branch  --recurse-submodules https://github.com/espressif/esp-adf
```

or old school

```
git clone  https://github.com/espressif/esp-adf
git checkout -t origin/<branch_name>
cd esp-adf
git submodule update --init --recursive
```

## Apply patches

In the esp-adf directory, you will see a patch directory idf_patches. The esp-idf version
pointed to is 5.3.1.

Thus,
```
cd esp-idf
git apply ..\idf_patches\idf_v5.3_freertos.patch
```

### downmix patch

`downmix.patch` fixes a bug in `esp-adf-libs` where the downmix element goes FINISHED
(and silences the output pipeline) when all input ringbufs are aborted — for example,
after stopping all track pipelines. The fix treats `AEL_IO_ABORT` the same as
`AEL_IO_TIMEOUT`: output silence for that slot, keep mixing. Only `AEL_IO_DONE` signals
a true end-of-stream.

Apply it from the `esp-adf-libs` component directory:

```
cd <esp-adf>/components/esp-adf-libs
git apply <path-to-murmura>/aithinker-adf/downmix.patch
```


## Environments

You'll be using the esp-idf which is checked in. Set the ESP_IDF env variable to that subdirectory.
Set the ESP_ADF variable to the level above.

Execute the the `install.ps1` and `export.ps1` **in the esp-idf directory**,
not the esp-adf directory (especially because they work with powershell)

## Overlay files

Copy the files under audio_board to components\audio_board . This will overwrite 

```
cp -r -Force audio_board <esp_adf>/components
```
(or similar)

Copy the es8388 file into components\audio_hal\driver\es8388 . It has that one
volume change that seemed necessary.

```
cp es8388/es8388.c <esp-adf>/components/audio_hal/driver/es8388
```

## menuconfig

When you do menuconfig, there are a few things to set.

I have checked in a defaults file that works with that version of esp-idf
in esp-adf that has the changes

1) the board hal to aithinker rev B
2) Enable PSRAM, turn on the task feature (this seems OK by default)
3) Set backward compatible freertos compeont -> freertos -> kernel ; set tick rate configTICK_RATE_HZ 250
4) set the ECO to 3.1 (component -> hardware settings -> chip rev -> minimum supported) ; XTAL frequency 40mhz
5) set the long file names to enabled (enabled on heap, default OK)
6) set the frequency to 240mhz (component -> esp system, ok by default)
7) set the flash size to 4mb, QIO, 80mhz speed -> ok by deafult
8) Change hostname to anything else (COmpoent -> LWIP)
9) Disable ESP Speech Recognition (saves memory)

**NOTE:** all of these are saved in the defaults in this directory, except the HAL.
Therefore I think you don't have to check these, but you probably should, eh?

## set efuse for flash

Remember also the thing about efuse, which you set once per board. 
The issue is there's a shared line,
and you need to set a particular efuse so you can use the quad-mode.

```
espefuse.py -p [board location eg COM4] set_flash_voltage 3.3V
```

## Set the DIP switches

IO13 set to data, IO15 set to cmd. 

DOWN - UP - UP - DOWN - DOWN

have to set the dip switches correctly. 

Basically IO13 is DATA or KEY.

IO15 is CMD (for the SD card) or jtag or something else.

