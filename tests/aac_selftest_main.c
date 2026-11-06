/* The helper's main() dispatch for --aac-selftest (AudioHelper.m), without the Objective-C rest of the helper. */
#include "AACSelfTest.h"
#include <stdio.h>
#include <string.h>
int main(int argc, const char **argv) {
    if (argc >= 2 && strcmp(argv[1], "--aac-selftest") == 0) return aac_selftest_main(argc, argv);
    fprintf(stderr, "usage: BTPSAudioHelper --aac-selftest [--stage=...] [--strategy=...]\n");
    return 2;
}
