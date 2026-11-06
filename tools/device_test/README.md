# Device test kit

Two questions, answered on the phone in one run, with no Bluetooth and nothing installed or changed:

1. **Why was the standalone test killed at launch (exit 137)?** The kit runs four launch controls side by side.
2. **Does Speaka's AAC decoder work on iOS?** `BTPSAudioHelper --aac-selftest` decodes an embedded ffmpeg AAC-LATM
   fixture (a 440 Hz tone) through the real `LATM.c`, `AACDecoder.c` and AudioToolbox, inside the installed helper -
   a binary already proven to launch on this phone, and the one that decodes AAC in production.

## Before you start

Install the Speaka build from the same CI run, either with **Update** in the Speaka app or by hand:

```powershell
$Phone = "mobile@10.5.58.216"
scp "$env:USERPROFILE\Downloads\com.parv.speaka_9.8.NN_iphoneos-arm64.deb" "${Phone}:/tmp/speaka.deb"
ssh -t $Phone 'sudo /var/jb/usr/bin/dpkg -i /tmp/speaka.deb'
```

Then download the `speaka-device-test-NN` artifact from the same run (Actions > the run > Artifacts) and unzip it.

## Run it (Windows PowerShell)

```powershell
$Phone = "mobile@10.5.58.216"
$Kit   = "$env:USERPROFILE\Downloads\speaka-device-test-NN"   # the unzipped artifact
$Out   = "$env:USERPROFILE\Desktop\speaka-device-test"

# 1. Copy the kit to the phone.
scp "$Kit\speaka-smoke-arm64" "$Kit\speaka-smoke-fat" "$Kit\speaka-device-test.sh" "${Phone}:/tmp/"

# 2. Run it. The summary prints here; the exit status is the number of unexpected results (0 = all as expected).
ssh $Phone 'sh /tmp/speaka-device-test.sh'
"remote exit status: $LASTEXITCODE"

# 3. Bring back every test's stdout, stderr and exit status.
scp -r "${Phone}:/tmp/speaka-device-test" $Out
```

The remote commands contain no double quotes or pipes on purpose: Windows PowerShell 5.1 strips embedded double
quotes from arguments to native programs, which is what turned `grep -E "A|B"` into `command not found` earlier.

## Reading the launch controls

| `helper-installed` | `helper-copy-in-tmp` | `speaka-smoke-arm64` | `speaka-smoke-fat` | Meaning |
| --- | --- | --- | --- | --- |
| ok | ok | 137 | ok | The arm64-only recipe was the cause; the fat (arm64 + arm64e) build launches. |
| ok | 137 | 137 | 137 | Executing from `/tmp` is refused; binaries must live under `/var/jb`. |
| ok | ok | 137 | 137 | Something else in the plain-clang recipe; compare `inspect.txt` with the helper's facts in the build log. |
| ok | ok | ok | ok | Launching works; the old binary itself was the problem (it was arm64-only and linked Foundation). |

A launch-time kill shows `exit=137 (signal 9)` within a second and no `TIMEOUT` line; a hang shows `TIMEOUT`.
`SMOKE_CTOR` in stderr means dyld loaded the binary; `SMOKE_MAIN` means `main()` ran.

## What a working AAC decoder looks like

`selftest-full` exits 0, and its strategy line for `raw-esds` (the production strategy) reads `pass=1`, with:

- `decoded=25 errors=0`, `frames=` at least 23552 (25 access units × 1024, minus up to two of priming);
- `hz=` about 440 and `snr_db=` at least 20 (the decoded audio fitted against the ideal 440 Hz tone; ffmpeg's own
  decoder scores 34.6 dB on this fixture, garbage scores near 0);
- `peak=` and `rms=` near ffmpeg's 2979 and 2049.

`selftest-full.out` holds the full JSON, including every AudioToolbox status: `specific_status` (Apple's software
AAC decoder), `new_status` (the fallback), `cookie_status`, and for the first failure `first_error_status`. If
`raw-esds` fails but another strategy passes, `working_strategies` names it.

If a stage fails, the last `STAGE` line in its `.err` file is where it stopped: `entry`, `fixture`,
`after_create ...` (converter creation and its statuses), `before_first_decode`, then one `DECODE` line per packet.
