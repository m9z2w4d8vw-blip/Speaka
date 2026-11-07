# The audio helper. Installed as /var/jb/usr/bin/BTPSAudioHelper, the name launchd starts (unchanged).
THEOS_PACKAGE_SCHEME ?= rootless
GO_EASY_ON_ME = 1
ARCHS := arm64 arm64e
TARGET := iphone:clang:latest:15.0
include $(THEOS)/makefiles/common.mk
TOOL_NAME = BTPSAudioHelper
BTPSAudioHelper_FILES = AudioHelper.m SBCDecoder.c AACDecoder.c AACSelfTest.c CodecDecoder.c AACConfig.c AACPacket.c LATM.c AudioRing.c ../shared/VolumeControl.c ../shared/VolumeMailbox.c vendor_sbc/sbc.c vendor_sbc/bits.c
BTPSAudioHelper_CFLAGS = -fobjc-arc -fblocks -std=gnu11 -I.. -I./vendor_sbc
BTPSAudioHelper_FRAMEWORKS = Foundation AVFoundation AudioToolbox
BTPSAudioHelper_INSTALL_PATH = /usr/bin
include $(THEOS_MAKE_PATH)/tool.mk
