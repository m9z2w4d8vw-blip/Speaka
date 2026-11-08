/* AACSelfTest.h - `SpeakaAudioHelper --aac-selftest`: decodes an embedded ffmpeg AAC-LATM fixture through the real
 * AACDecoder / LATM / AudioConverter path on the device and prints a JSON report. No Bluetooth involved. */
#pragma once
int aac_selftest_main(int argc, const char **argv);
