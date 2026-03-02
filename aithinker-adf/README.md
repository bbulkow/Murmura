# To make a program with ESP-ADF and the AIThinker

Copy the files under audio_board to components\audio_board

Copy the es8388 file into components\audio_hal\driver\es8388 . It has that one
volume change that seemed necessary.

Use esp-adf version, I used branch 2.7 (because it'll be well known instead of master).

Clone that version, then do the submodule update. You'll be using the
esp-idf which is checked in. Set the ESP_IDF env variable to there.
Set the ESP_ADF variable to the level above.
Use the `install.ps1` and `export.ps1` that is in the esp-idf directory,
not the esp-adf directory (especially because they work with powershell)

When you do menuconfig, there are a few things to set.

I have checked in a defaults file that works with that version of esp-idf
in esp-adf that has the changes

1) the board hal to aithinker rev B
2) Enable PSRAM, turn on the task feature
3) set the ECO to 3.1
4) set the long file names to enabled
5) set the frequency to 240mhz
6) set the flash size to 4mb, QIO, 80mhz speed

Remember also the thing about efuse, which you set once per board. 
The issue is there's a shared line,
and you need to set a particular efuse so you can use the quad-mode.

the command is:
espefuse.py -p COM4 set_flash_voltage 3.3V

And you have to set the dip switches correctly. There's basically JTAG
mode and the mode where the SDcard works. And, there's where KEY2 works,
or where SD card works. You want the sd card. IO13 set to data2, IO15 set
to CMD. 