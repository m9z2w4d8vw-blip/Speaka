/* The smallest possible iOS command-line program: no frameworks beyond libSystem, no buffering. It only answers
 * "can this build-and-deploy recipe launch a binary on the phone at all?", separately from AudioToolbox or Speaka.
 * SMOKE_CTOR proves dyld loaded the binary and ran its initializers; SMOKE_MAIN proves main() was reached. */
#include <stdio.h>
#include <unistd.h>

__attribute__((constructor)) static void smoke_ctor(void) {
    static const char line[] = "SMOKE_CTOR\n";
    ssize_t ignored = write(STDERR_FILENO, line, sizeof line - 1);
    (void)ignored;
}

int main(int argc, char **argv) {
    char line[512];
    int n = snprintf(line, sizeof line, "SMOKE_MAIN pid=%d argc=%d exe=%s\n", (int)getpid(), argc, argc ? argv[0] : "?");
    if (n > 0) { ssize_t ignored = write(STDERR_FILENO, line, (size_t)n); (void)ignored; }
    static const char ok[] = "SMOKE_OK\n";
    ssize_t ignored = write(STDOUT_FILENO, ok, sizeof ok - 1);
    (void)ignored;
    return 0;
}
