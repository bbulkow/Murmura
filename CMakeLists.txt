# The following lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.5)

# I'm not sure why there's no O3 available on this platform it's been standard 3 decades
# 
# add_compile_options(-O3)
# idf_build_set_property(COMPILE_OPTIONS "-O3" APPEND)

include($ENV{ADF_PATH}/CMakeLists.txt)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

project(play_sdcard_music)
