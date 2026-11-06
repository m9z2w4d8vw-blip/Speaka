# Testing experimental AAC-LC on a device

AAC is **off by default** and gated behind the `SpeakaAAC` preference. SBC is unchanged; follow this only to exercise
the AAC groundwork. Read the AAC blockers in [CHANGELOG.md](CHANGELOG.md) first: until a second, AAC endpoint is
registered in `bluetoothd` (the native blocker), **Windows will negotiate SBC, not AAC**, so the codec line below is
expected to read `sbc`. This procedure is written so that the day an AAC endpoint lands, confirming it is one step.

## 1. Turn the switch on

```sh
ssh mobile@<iphone-ip>
# as mobile (the helper and Settings read com.apple.bluetooth as the user):
defaults write com.apple.bluetooth SpeakaAAC -bool YES
# turn it back off with: defaults write com.apple.bluetooth SpeakaAAC -bool NO
```

Then tap **Start** in the app and play from the PC, as for SBC.

## 2. What the helper logs about the codec

Each session the helper prints one `V93_CODEC` line near the start of playback (it decides the codec from the ring
header the tweak set at CONFIGURE):

```
HH:MM:SS.mmm V93_CODEC sbc
```

Meaning:
- `V93_CODEC sbc` - the session is SBC. This is the current expected result, because no AAC endpoint is advertised.
- `V93_CODEC aac rate=48000 channels=2` - the session negotiated AAC-LC and the AAC decoder was built. Only possible
  once an AAC endpoint is registered.
- `V93_CODEC aac negotiated but SpeakaAAC off; no audio (not decoding AAC as SBC)` - AAC negotiated, switch off.
- `V93_CODEC aac config invalid/unsupported; no audio` or `... aac decoder setup failed; no audio` - AAC negotiated
  but undecodable. The helper stays silent rather than misreading AAC as SBC; fix the config or the decoder. (This
  only arises once an AAC endpoint exists; with none, sessions are SBC and play normally.)

Read it live:

```sh
# on the phone, tail the helper log (path from shared/SpeakaShared.h):
tail -f /tmp/FTABHarvest/BTPSAudioHelper-v88.log | grep -E "V93_CODEC|V91_SESSION|V881_PCM"
```

Or in the app: **Settings > Speaka > View debug log**, then **Save Log to iCloud Drive**. The `V93_CODEC` line is in
the `AUDIO HELPER` section.

## 3. Confirm what was really negotiated (protocol truth, independent of the logs)

A `V93_CODEC` line reflects what the tweak wrote into the ring header. To confirm the actual AVDTP negotiation on the
wire, capture it rather than trusting the daemon:

- On **Windows**, a Bluetooth HCI capture (e.g. the Bluetooth Test Platform `btvs`/`btetlparse`, or Wireshark with a
  live HCI interface) shows the AVDTP SET_CONFIGURATION. The Media Codec capability's **Media Codec Type** byte is
  `0x00` for SBC and `0x02` for MPEG-2/4 AAC. That byte is the ground truth for which codec was chosen.
- The iPhone side cannot show this from userspace without the native endpoint work; the capture is the right source.

A packet capture establishes the **codec that was negotiated**. It does not reveal `bluetoothd`'s internal
endpoint-table layout - that is the native side, and recovering it is the blocker, not something a capture provides.

## 4. If you are developing the AAC endpoint

The protocol and decoder are ready and tested on the host (`tests/test_aac.py`). What remains is native and is
deliberately not implemented here:

1. Register a second SEP whose Media Codec capability is `sink_build_aac_codec_record()` (already built), the way
   `tweak/Hooks.xm` registers the SBC Sink after the six native Sources.
2. In the CONFIGURE hook, when the chosen SEID is the AAC endpoint, parse the SRC's 6-byte AAC Codec Specific
   Information with `aac_parse_config`, and call `AudioRelaySetCodec(A2DP_CODEC_AAC, csi, 6)` instead of the SBC
   default. The helper then builds the AAC decoder automatically.
3. Re-run this procedure; `V93_CODEC aac ...` and a `0x02` codec byte in the capture confirm end to end.

Steps 1 and 2's endpoint-table writes require the native layout for an AAC endpoint, which is the reverse-engineering
blocker. Everything they would call into already exists and is tested.
