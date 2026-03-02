# To make a program with ESP-ADF and the AIThinker

Copy these files to esp-adf/components/audio_board

Use esp-adf version, not sure which. Tested 2.7 at the moment.

Clone that version, then do the submodule update. You'll be using the
esp-idf which is checked in. Set the ESP_IDF env variable to there.
Set the ESP_ADF variable to the level above.
Use the `install.ps1` and `export.ps1` that is in the esp-idf directory,
not the esp-adf directory (especially because they work with powershell)

When you do menuconfig, there are a few things to set.

1) the board hal to aithinker rev A
2) Enable PSRAM
3) set the ECO to 3.1
4) set the long file names to enabled
5) set the frequency to 240mhz
6) set the flash size to 4mb, QIO, 80mhz speed

Remember also the thing about efuse. The issue is there's a shared line,
and you need to set a particular efuse so you can use the quad-mode.

And you have to set the dip switches correctly. There's basically JTAG
mode and the mode where the SDcard works. And, there's where KEY2 works,
or where SD card works. You want the sd card.