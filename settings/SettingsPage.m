// SettingsPage.m - the Speaka page in Settings: what the tweak reports, and its debug log. Start, Stop and the audio
// output are in the Speaka app (since v9.7), so this page only reads.
//
// PSListController lives in the private Preferences framework, which is not linked here (the CI build only has the
// public Xcode SDK). It is declared by hand instead and resolved at runtime from the Settings process, which has
// already loaded it. The bundle is linked with -undefined dynamic_lookup for exactly this reason.
#import <UIKit/UIKit.h>
#import "DebugLog.h"

@interface PSListController : UIViewController {
@protected
    NSArray *_specifiers;
}
- (NSArray *)specifiers;
- (NSArray *)loadSpecifiersFromPlistName:(NSString *)plistName target:(id)target;
- (void)reloadSpecifiers;
@end

@interface SpeakaSettingsController : PSListController
@end

@implementation SpeakaSettingsController

- (NSArray *)specifiers
{
    if (!_specifiers) {
        _specifiers = [self loadSpecifiersFromPlistName:@"Root" target:self];
    }
    return _specifiers;
}

- (void)viewWillAppear:(BOOL)animated
{
    [super viewWillAppear:animated];
    [self reloadSpecifiers];           // refresh the status rows every time the page is shown
}

#pragma mark - values for the rows in Root.plist (the "get" selectors)

- (id)loadedValue:(id)specifier       { return [[SpeakaLogReader shared] loadedSummary]; }
- (id)lastEventValue:(id)specifier    { return [[SpeakaLogReader shared] lastEventSummary]; }
- (id)channelValue:(id)specifier      { return [[SpeakaLogReader shared] channelSummary]; }

#pragma mark - buttons (the "action" selectors)

- (void)showLog:(id)specifier
{
    [self.navigationController pushViewController:[[SpeakaLogViewController alloc] init] animated:YES];
}

- (void)copyLog:(id)specifier
{
    [UIPasteboard generalPasteboard].string = [[SpeakaLogReader shared] fullText];
    [self say:@"Copied the debug log to the clipboard."];
}

- (void)clearLog:(id)specifier
{
    [self say:[[SpeakaLogReader shared] clearAll]];
    [self reloadSpecifiers];
}

#pragma mark - alerts

- (void)say:(NSString *)message
{
    UIAlertController *alert = [UIAlertController alertControllerWithTitle:nil message:message
                                                            preferredStyle:UIAlertControllerStyleAlert];
    [alert addAction:[UIAlertAction actionWithTitle:@"OK" style:UIAlertActionStyleDefault handler:nil]];
    [self presentViewController:alert animated:YES completion:nil];
}

@end
