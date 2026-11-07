/* UpdatePolicy.h - which release Speaka announces (v9.9.3), shared by SpeakaUpdateCheck and the host tests; the app's
 * Swift (SpeakaUpdater.versionParts / compare) follows the same rules.
 *
 * Versions compare by their numeric parts, anything else separating them: "v9.9.101" = [9, 9, 101] > "9.9.100".
 * A release is announced when it carries the app, is newer than what's installed, and isn't the one announced
 * already. An unknown installed version announces nothing: it can't be told newer. */
#ifndef SPEAKA_UPDATE_POLICY_H
#define SPEAKA_UPDATE_POLICY_H

#ifdef __cplusplus
extern "C" {
#endif

int sp_version_compare(const char *a, const char *b);    /* <0, 0, >0 */
int sp_update_should_announce(const char *installed, const char *newest, const char *newest_title,
                              const char *announced);

#ifdef __cplusplus
}
#endif

#endif
