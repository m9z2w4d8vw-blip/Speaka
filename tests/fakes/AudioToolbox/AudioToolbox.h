/* A stand-in for the PUBLIC AudioConverter API, for hosts without Apple's SDK (Linux). Signatures follow Apple's
 * AudioToolbox headers. On macOS the tests use the real framework instead; this file is never used there or in any
 * shipped build. The implementation (fake_audiotoolbox.c) records what the decoder hands the converter so the tests
 * can check the contract - one bare access unit per packet, a packet description, the class description and cookie. */
#pragma once
#include <stdint.h>
#include <stddef.h>
typedef int32_t OSStatus;
typedef uint32_t UInt32;
typedef uint32_t OSType;
typedef double Float64;
enum { noErr = 0 };
typedef struct { Float64 mSampleRate; UInt32 mFormatID, mFormatFlags, mBytesPerPacket, mFramesPerPacket,
                 mBytesPerFrame, mChannelsPerFrame, mBitsPerChannel, mReserved; } AudioStreamBasicDescription;
typedef struct { UInt32 mNumberChannels, mDataByteSize; void *mData; } AudioBuffer;
typedef struct { UInt32 mNumberBuffers; AudioBuffer mBuffers[1]; } AudioBufferList;
typedef struct { int64_t mStartOffset; UInt32 mVariableFramesInPacket, mDataByteSize; } AudioStreamPacketDescription;
typedef struct { OSType mType, mSubType, mManufacturer; } AudioClassDescription;
typedef struct OpaqueAudioConverter *AudioConverterRef;
typedef UInt32 AudioConverterPropertyID;
enum { kAudioFormatLinearPCM = 0x6c70636d, kAudioFormatMPEG4AAC = 0x61616320 };
enum { kAudioFormatFlagIsSignedInteger = 4, kAudioFormatFlagIsPacked = 8, kMPEG4Object_AAC_LC = 2 };
enum { kAudioConverterDecompressionMagicCookie = 0x646d6763 };
typedef OSStatus (*AudioConverterComplexInputDataProc)(AudioConverterRef, UInt32 *, AudioBufferList *,
                                                       AudioStreamPacketDescription **, void *);
OSStatus AudioConverterNew(const AudioStreamBasicDescription *, const AudioStreamBasicDescription *, AudioConverterRef *);
OSStatus AudioConverterNewSpecific(const AudioStreamBasicDescription *, const AudioStreamBasicDescription *, UInt32,
                                   const AudioClassDescription *, AudioConverterRef *);
OSStatus AudioConverterDispose(AudioConverterRef);
OSStatus AudioConverterReset(AudioConverterRef);
OSStatus AudioConverterSetProperty(AudioConverterRef, AudioConverterPropertyID, UInt32, const void *);
OSStatus AudioConverterFillComplexBuffer(AudioConverterRef, AudioConverterComplexInputDataProc, void *, UInt32 *,
                                         AudioBufferList *, AudioStreamPacketDescription *);
