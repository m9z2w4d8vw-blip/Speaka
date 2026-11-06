# On-device AAC decode test

A standalone iOS command-line binary that decodes a real AAC-LATM fixture through Speaka's actual `AACDecoder.c`,
`LATM.c`, `AACConfig.c`, and the **real** AudioToolbox `AudioConverter`. No stubs, no Bluetooth, no `bluetoothd`
changes. It is the only build that actually runs the AudioConverter path, which CI otherwise compiles but never
executes.

It answers one question: **does Speaka's AAC decoder turn real A2DP-shaped AAC-LATM into correct PCM on the phone?**
That is independent of the endpoint-registration blocker (which is why Windows still negotiates SBC).

## What it does

1. Embeds the ffmpeg-generated LATM fixture (`tests/fixtures/aac_latm_48k_stereo.loas`) at build time, so the binary
   is self-contained.
2. Strips LOAS framing to recover the bare AudioMuxElements A2DP would deliver.
3. Feeds each as an `AudioPacket` to the real decoder, exactly as the helper's receive loop does.
4. Prints a JSON report: decoder status, element/decoded/error counts, PCM frame count, peak and RMS amplitude, the
   decoded rate and channels, and a `pass` verdict.

## Build

CI builds it on the macOS runner against the iPhoneOS SDK and uploads it as the `aac-decode-test` artifact. Download
that artifact, or build locally on a Mac with the iPhoneOS SDK:

```sh
make -C tools/aac_decode_test        # produces tools/aac_decode_test/aac_decode_test (signed arm64)
```

## Deploy and run over SSH (Windows PowerShell)

Replace the path to `aac_decode_test` with wherever you downloaded or built it.

```powershell
$Phone = "mobile@10.5.58.216"
$Bin   = "$env:USERPROFILE\Downloads\aac_decode_test"   # the CI artifact, unzipped

# Copy the test to the phone and make it runnable.
scp $Bin "${Phone}:/tmp/aac_decode_test"
ssh $Phone 'chmod +x /tmp/aac_decode_test'

# Run it and capture the JSON report.
ssh $Phone '/tmp/aac_decode_test' | Tee-Object "$env:USERPROFILE\Desktop\aac_decode_result.json"
```

If TrollStore's environment requires it, the binary is already `ldid -S` signed by the build; no entitlements are
needed because it only reads an embedded buffer and calls AudioToolbox.

## What a pass looks like

The last line is `"pass": true`, with, for the 48 kHz stereo fixture:

- `"decoder_created": true` — AudioConverter accepted the AAC-LC config;
- `"decoded_ok"` close to `"expected_elements"` (25), `"errors": 0`;
- `"pcm_frames"` around 25 × 1024 = 25600 (AAC-LC emits 1024 frames per access unit, minus any priming);
- `"decoded_rate": 48000`, `"decoded_channels": 2`;
- `"peak"` and `"rms"` clearly non-zero (the fixture is a 440 Hz tone).

That demonstrates the decoder produces correct, non-silent PCM from real AAC-LATM on the hardware. A failure
(`"pass": false`) shows which check failed and the first decoder error code, which maps to `AACDecoder.c`'s return
values (for example `-11` = LATM parse failed, `-6` = AudioConverter error).

## What it does NOT prove

It does not prove Windows will negotiate AAC — that still needs the second Sink endpoint in `bluetoothd`. It proves
the decode half works, so that once the endpoint lands, the path is known-good end to end.
