# iOS Bluetooth / AVRCP reference binaries

These files were supplied separately for **offline reverse-engineering reference**. They are not source files, link inputs, dependencies, or runtime assets for Speaka. The project build does not refer to this directory.

The source paths below describe the locations from which the binaries were collected on the user's iPhone (reported iPhone 13, iOS 17.0); this ZIP does not independently attest to their operating-system build. These files must not be executed on a development host.

| File | Original device path | Bytes | SHA-256 |
|---|---|---:|---|
| `BTAvrcp` | `/usr/sbin/BTAvrcp` | 174,800 | `4bd97270734de51a461fd787aac6b76b574df656c5c234817b8bbd8bdb1bfa61` |
| `bluetoothd` | `/usr/sbin/bluetoothd` | 10,196,720 | `e9408df54c3eebfd263eb745a0b06cf1130dd3228f56c104697f92264c91dbd6` |
| `bluetoothuserd` | `/usr/libexec/bluetoothuserd` | 703,008 | `5c86b91bad94d252e11378ef13610140e800d3122e0e462b15e2041aca920fc5` |
| `BTAudioHALPlugin` | `/System/Library/Audio/Plug-Ins/HAL/BTAudioHALPlugin.driver/BTAudioHALPlugin` | 612,784 | `5194ccb2c0168bb7a5b70ee039024b194a5497b66dc5fc8fa72e21cb8ede1f68` |
| `mediaremoted` | `/System/Library/PrivateFrameworks/MediaRemote.framework/Support/mediaremoted` | 2,983,744 | `4df1a15e29f01a9e2e43d865c08f3c1ff21b5461277451cae96065c979a7e807` |
| `avrcp-launchd.plist` | `/System/Library/LaunchDaemons/com.apple.BTServer.avrcp.plist` | 278 | `6d03f9801e9b64d34808a2129e81bbdca9514915f29ae510c5af49458e0d9c36` |

The `.plist` is a binary Apple property list, not executable code. The other five files are Mach-O binaries. They are preserved byte-for-byte from `Speaka-AVRCP-Analysis.zip`.

**Build isolation:** all contents are under `docs/reference-binaries/`; no Makefiles, build scripts, app resources, tweak files, or existing documentation have been changed.
