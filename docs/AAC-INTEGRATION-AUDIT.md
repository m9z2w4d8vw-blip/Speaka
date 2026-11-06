# AAC integration audit

Where experimental AAC stands, what is verified, what is assumed, and the minimum remaining work for real Bluetooth
AAC reception. Verified means checked against the code or a run; assumed means inferred from the spec or open-source
practice and not yet confirmed on this build.

## Decode path (the half that does not touch bluetoothd)

| Piece | State | Evidence |
| --- | --- | --- |
| AAC capability record | **Verified built** | `sink_build_aac_codec_record` (tested); it is not advertised anywhere yet |
| AAC config parse | **Verified** | `aac_parse_config` tested, including rejecting ranges and unsupported rates |
| LATM depacketization | **Verified against ffmpeg** | `latm_parse_audiomuxelement` extracts 25/25 access units byte-identical to ffmpeg's ADTS encode of the same audio |
| AAC-LC decode to PCM | **Verified on macOS in CI; device run pending** | The converter gets bare access units and an ESDS cookie (an earlier version wrongly sent ADTS headers). CI's device-test job decodes the fixture with the real AudioToolbox and compares with ffmpeg; the phone runs the same code through `BTPSAudioHelper --aac-selftest` |
| Codec dispatch + safe fallback | **Verified** | `codec_decoder_begin` tested: SBC stays SBC; undecodable AAC goes to a silent DISABLED state, never SBC |
| Ring carries the codec | **Verified** | `AudioRing.codec` / `codec_config` set by `AudioRelaySetCodec`, read by the helper; handshake size check passed on device (ring is 78984 bytes and the helper mapped it) |

## Endpoint registration (the half that needs bluetoothd internals)

**How SEID 7 (the SBC Sink) is registered today, verified from `tweak/Hooks.xm`:**
- The config-getter hook is armed for one restart; it gives the endpoint table a 7th slot (`V78_TABLE RESULT slots=7`).
- After iOS's six native Source endpoints register, the tweak writes a Sink endpoint into slot 7: SEID 7, the SBC
  codec capability bytes, and the audio-context table pointer and counts, all at offsets recovered from one exact
  `bluetoothd` build. The SDP record (`0x110B`) is published through the SDP-add hook.
- At CONFIGURE, `hook_setconfigind` matches `a3 == 7` (the local SEID) and arms the relay for that stream.

**Can this infrastructure support a second (AAC) Sink endpoint?**
- *Assumption, not verified:* the table has room (it was grown to 7 slots with 6 occupied, so one free slot exists;
  a second Sink would need an 8th slot, i.e. growing the table by two, not one). The current code grows it by one.
- *Verified blocker:* registering any Sink endpoint depends on the native audio-context table layout (slot stride,
  SEID offset, codec-pointer offset, count fields) recovered for this one build. A second endpoint needs the same
  layout applied twice, and the capability bytes for AAC rather than SBC. That layout is only knowable by
  disassembling the signed `bluetoothd`, which is the reverse-engineering line not crossed here.
- *Isolated behind an interface:* the AAC capability record is `sink_build_aac_codec_record()` (built and tested).
  Everything that does not need the native layout is done; only the table-write for a second endpoint is missing.

**Identifying the chosen codec at CONFIGURE (implemented):**
- *Verified:* `hook_setconfigind` already sees the local SEID (`a3`). With one Sink (SEID 7 = SBC) the codec is
  always SBC, which is why `AudioRelaySetCodec(A2DP_CODEC_SBC, ...)` is called unconditionally.
- *Implemented, protocol level:* the verified RX signaling hook already copies the PC's SET_CONFIGURATION for
  SEID 7; `avdtp_setconfig_media_codec` (StreamControl.c) reads its Media Codec capability per AVDTP 8.9/8.21, and
  `hook_setconfigind` publishes that codec (AAC with its 6 Codec Specific bytes) instead of assuming SBC. Logged as
  `V93_CONFIG_RX` and `V88_CONFIG ... codec=`. With only the SBC endpoint registered it reads SBC.

**Does the config reach the helper before decoding?**
- *Verified:* `AudioRelaySetCodec` writes `codec` and `codec_config` into the ring header before
  `AudioRelaySetActive(1)` flips the generation active. The helper reads the header when it sees the active
  generation, in `codec_decoder_begin`, before the first `codec_decoder_decode`. Release/acquire ordering on
  `generation` places the plain header writes ahead of the helper's read.

## Stream handling for real AAC

| Concern | State | Notes |
| --- | --- | --- |
| Ring payload capacity | **Verified sufficient** | 2048-byte payload; AAC-LC is ≤~853 bytes/access unit at 320 kbps. Fixture elements are 296–401 bytes. |
| One access unit per packet | **Verified for A2DP** | A2DP sends one AudioMuxElement per media packet; `aac_decoder_decode` handles one element per call. |
| Decoder state across frames | **Verified** | `AudioConverterReset` on generation change / sequence gap, matching the SBC decoder's reset points. |
| Config change mid-stream | **Implemented** | An in-band StreamMuxConfig that differs from the converter's rebuilds it; a new rate reaches the helper, which restarts its output. Unsupported configs are refused (-14). |
| LATM fragmentation across packets | **Implemented** | Fragments share an RTP timestamp (RFC 3016); the decoder reassembles them, and a lost fragment costs only its own element. |
| Multiple access units per element | **Out of scope, rejected** | A2DP uses one AU per element; the parser handles one and rejects `numSubFrames != 0`. |

## Minimum remaining work for real Bluetooth AAC

1. **Run the device test kit** (`tools/device_test/README.md`) and confirm `"pass": true`: AudioConverter decoding
   AAC-LC to PCM on iOS. No Bluetooth needed.
2. **Register a second AAC Sink endpoint** in `tweak/Hooks.xm` (an eighth table slot; a SEID with
   `sink_build_aac_codec_record()`'s capability bytes, written with the native layout). **This needs the reverse
   engineering not done here** - the only remaining blocker.
3. **Accept that SEID** where the hooks match SEID 7 (`a3 == 7u` in `hook_setconfigind`, `(control[2]>>2)==7u` in
   `hook_receive_signal`, `local_seid=7`). Everything after CONFIGURE - codec identification, the codec in the ring,
   the decoder - already works for whichever codec the PC chooses.
