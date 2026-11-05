/* BTPSAudioHelper: plays the audio bluetoothd hands it (BTPSV88Stream.m). launchd starts it on demand, as mobile,
 * when the tweak connects to 127.0.0.1:37594 (layout/Library/LaunchDaemons/com.parv.speaka.audio.plist).
 * The v7.3-v7.6 test modes, which played test PCM from Settings or bluetoothd and decoded a recorded SBC fixture
 * when started by hand over SSH, were removed in v9.8. */
#import <Foundation/Foundation.h>
#import <stdio.h>
#import <string.h>
#import "BTPSV88Stream.h"

int main(int argc, const char **argv) {
    @autoreleasepool {
        if (argc == 2 && strcmp(argv[1], "--stream-service") == 0) return BTPSV88StreamMain();
        fprintf(stderr, "usage: BTPSAudioHelper --stream-service (launchd starts it: com.parv.speaka.audio)\n");
        return 2;
    }
}
