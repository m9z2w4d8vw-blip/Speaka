/* UpdatePolicy.c - see UpdatePolicy.h. */
#include "UpdatePolicy.h"

#include <string.h>

/* The next numeric part at *p, advancing past it; -1 when there is none left. Parts over 9 digits are clamped. */
static long next_part(const char **p) {
    const char *s = *p;
    while (*s && (*s < '0' || *s > '9')) s++;
    if (!*s) { *p = s; return -1; }
    long n = 0;
    int digits = 0;
    for (; *s >= '0' && *s <= '9'; s++)
        if (++digits <= 9) n = n * 10 + (*s - '0');
    *p = s;
    return n;
}

int sp_version_compare(const char *a, const char *b) {
    const char *x = a ? a : "", *y = b ? b : "";
    for (;;) {
        long u = next_part(&x), v = next_part(&y);
        if (u < 0 && v < 0) return 0;
        if (u < 0) u = 0;            /* a missing part counts as 0: "9.9" == "9.9.0" */
        if (v < 0) v = 0;
        if (u != v) return u < v ? -1 : 1;
    }
}

static int has_digit(const char *s) {
    for (; s && *s; s++)
        if (*s >= '0' && *s <= '9') return 1;
    return 0;
}

int sp_update_should_announce(const char *installed, const char *newest, const char *newest_title,
                              const char *announced) {
    if (!has_digit(installed) || !has_digit(newest)) return 0;
    if (sp_version_compare(newest, installed) <= 0) return 0;
    if (announced && newest_title && strcmp(announced, newest_title) == 0) return 0;
    return 1;
}
