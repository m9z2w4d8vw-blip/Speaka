# Build the standalone on-device AAC decode test against the real iPhoneOS SDK.
#
#   make                     # builds ./aac_decode_test, a signed arm64 iOS command-line binary
#   make clean
#
# It links the real AudioToolbox and compiles Speaka's actual AAC sources (no stubs). The fixture is embedded by
# make_fixture_header.py, so the binary is self-contained: push it to the phone and run it over SSH.

SDK      := $(shell xcrun --sdk iphoneos --show-sdk-path)
CLANG    := $(shell xcrun --sdk iphoneos --find clang)
ARCH     := -arch arm64
MINVERS  := -miphoneos-version-min=14.0

ROOT     := ../..
SRCS     := aac_decode_test.m \
            $(ROOT)/audiohelper/AACDecoder.c \
            $(ROOT)/shared/LATM.c \
            $(ROOT)/shared/AACConfig.c
# AACDecoder.c pulls in AudioToolbox and LATM only; no libsbc, so the AAC test needs no SBC sources.

CFLAGS   := -isysroot $(SDK) $(ARCH) $(MINVERS) -I$(ROOT)/audiohelper -I$(ROOT)/shared \
            -fobjc-arc -Wall -Wextra -O2
LDFLAGS  := -isysroot $(SDK) $(ARCH) $(MINVERS) -framework Foundation -framework AudioToolbox

aac_decode_test: fixture_data.h
	$(CLANG) $(CFLAGS) $(SRCS) $(LDFLAGS) -o $@
	ldid -S $@

fixture_data.h:
	python3 make_fixture_header.py

clean:
	rm -f aac_decode_test fixture_data.h

.PHONY: clean
