# Notes for Agents (Claude, Gemini, GPT)

After making code changes, run code to make sure it works as best as possible.

For esp-idf components, run build and make sure all compile errors are removed.

There are a series of warnings regarding obsolete drivers, those are acceptable

# Environment

You are running in powershell 7 on windows.

# Running esp-idf

The environment variables ADF_PATH , IDF_PATH, and IDF_TOOLS path are correctly configured.

The working environment is in ~/dev/esp/esp-adf/esp-idf . The tools directory is configured as ~/dev/esp/esp-idf/tools . 

The espressif extension is configured correctly, and its' configuration is in the Tools 

# running device manager and scape-server

For the device-manager and scape-server, please execute a set of python commands to make sure the basic function is correct.