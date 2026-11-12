import ExpoModulesCore

/// Hands background URLSession wake-ups to UploadManager (app relaunched to finish uploads).
public class ScannerCaptureAppDelegate: ExpoAppDelegateSubscriber {
  public func application(_ application: UIApplication,
                          didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
    UploadManager.shared.activate()
    return true
  }

  public func application(_ application: UIApplication, handleEventsForBackgroundURLSession identifier: String,
                          completionHandler: @escaping () -> Void) {
    guard identifier == UploadManager.sessionIdentifier else { return }
    UploadManager.shared.backgroundCompletionHandler = completionHandler
    UploadManager.shared.activate()
  }
}
