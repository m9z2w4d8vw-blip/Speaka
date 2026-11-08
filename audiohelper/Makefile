# The audio helper, installed as /var/jb/usr/bin/SpeakaAudioHelper: launchd starts it as com.parv.speaka.audio.
THEOS_PACKAGE_SCHEME ?= rootless
GO_EASY_ON_ME = 1
ARCHS := arm64 arm64e
TARGET := iphone:clang:latest:15.0
include $(THEOS)/makefiles/common.mk
TOOL_NAME = SpeakaAudioHelper
SpeakaAudioHelper_FILES = AudioHelper.m SBCDecoder.c AACDecoder.c AACSelfTest.c CodecDecoder.c ../shared/AACConfig.c ../shared/AACPacket.c ../shared/LATM.c ../shared/AudioRing.c ../shared/VolumeControl.c ../shared/VolumeMailbox.c vendor_sbc/sbc.c vendor_sbc/bits.c
SpeakaAudioHelper_CFLAGS = -fobjc-arc -fblocks -std=gnu11 -I.. -I./vendor_sbc
SpeakaAudioHelper_FRAMEWORKS = Foundation AVFoundation AudioToolbox
SpeakaAudioHelper_INSTALL_PATH = /usr/bin
include $(THEOS_MAKE_PATH)/tool.mk
