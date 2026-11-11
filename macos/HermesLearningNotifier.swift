import AppKit
import UserNotifications

private struct Receipt: Decodable {
    let summary: String
    let text: String

    init(at url: URL) throws {
        self = try JSONDecoder().decode(Self.self, from: Data(contentsOf: url))
    }
}

private final class Notifier: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()
    private let openAction = "view-learning-receipt"
    private let category = "learning-receipt"
    private var window: NSWindow?

    func applicationWillFinishLaunching(_ notification: Notification) {
        center.delegate = self
        let view = UNNotificationAction(identifier: openAction, title: "查看学习明细", options: .foreground)
        center.setNotificationCategories([UNNotificationCategory(identifier: category, actions: [view], intentIdentifiers: [])])
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let arguments = Array(CommandLine.arguments.dropFirst())
        if arguments == ["--authorize"] {
            center.requestAuthorization(options: .alert) { allowed, error in
                self.finish(error?.localizedDescription ?? (allowed ? nil : "已关闭 Hermes 学习通知"))
            }
        } else if arguments.count == 2 && arguments[0] == "--post" {
            post(URL(fileURLWithPath: arguments[1]))
        } else if arguments.count == 2 && arguments[0] == "--prepare-upgrade" {
            prepareUpgrade(at: URL(fileURLWithPath: arguments[1]))
        }
    }

    private func prepareUpgrade(at url: URL) {
        let target = url.standardizedFileURL.resolvingSymlinksInPath()
        let previous = NSWorkspace.shared.runningApplications.filter {
            $0.processIdentifier != ProcessInfo.processInfo.processIdentifier &&
                $0.bundleURL?.standardizedFileURL.resolvingSymlinksInPath() == target
        }
        guard previous.allSatisfy({ $0.terminate() }) else {
            finish("无法退出正在运行的 Hermes 学习，请关闭明细窗口后重试安装")
            return
        }
        DispatchQueue.global().async {
            let deadline = Date().addingTimeInterval(10)
            while previous.contains(where: { !$0.isTerminated }) && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.05)
            }
            self.finish(previous.contains(where: { !$0.isTerminated }) ? "Hermes 学习尚未退出，安装已取消" : nil)
        }
    }

    private func post(_ url: URL) {
        do {
            let receipt = try Receipt(at: url)
            center.getNotificationSettings { settings in
                guard settings.authorizationStatus == .authorized || settings.authorizationStatus == .provisional else {
                    self.finish("请在系统设置的通知中允许「Hermes 学习」发送通知")
                    return
                }
                let content = UNMutableNotificationContent()
                content.title = "学习回执"
                content.body = receipt.summary
                content.categoryIdentifier = self.category
                content.userInfo = ["receiptPath": url.path]
                let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false)
                let request = UNNotificationRequest(identifier: url.deletingPathExtension().lastPathComponent,
                                                    content: content, trigger: trigger)
                self.center.add(request) { error in self.finish(error?.localizedDescription) }
            }
        } catch {
            finish(error.localizedDescription)
        }
    }

    private func finish(_ error: String?) {
        if let error {
            FileHandle.standardError.write(Data((error + "\n").utf8))
        }
        exit(error == nil ? 0 : 1)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        guard response.actionIdentifier == UNNotificationDefaultActionIdentifier || response.actionIdentifier == openAction,
              let path = response.notification.request.content.userInfo["receiptPath"] as? String else {
            completionHandler()
            return
        }
        DispatchQueue.main.async {
            self.showReceipt(URL(fileURLWithPath: path))
            completionHandler()
        }
    }

    private func showReceipt(_ url: URL) {
        let frame = NSRect(x: 0, y: 0, width: 660, height: 510)
        let scroll = NSScrollView(frame: frame)
        scroll.hasVerticalScroller = true
        let text = NSTextView(frame: scroll.bounds)
        text.string = (try? Receipt(at: url).text) ?? "这条通知对应的本地回执已无法读取。"
        text.font = .systemFont(ofSize: 14)
        text.isEditable = false
        text.isRichText = false
        text.textContainerInset = NSSize(width: 22, height: 18)
        text.isVerticallyResizable = true
        text.autoresizingMask = .width
        text.textContainer?.widthTracksTextView = true
        scroll.documentView = text

        let viewer = window ?? NSWindow(contentRect: frame, styleMask: [.titled, .closable, .resizable, .miniaturizable],
                                        backing: .buffered, defer: false)
        viewer.isReleasedWhenClosed = false
        viewer.title = "Hermes 学习明细"
        viewer.contentView = scroll
        if window == nil { viewer.center() }
        window = viewer
        NSApp.activate(ignoringOtherApps: true)
        viewer.makeKeyAndOrderFront(nil)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
private let notifier = Notifier()
app.delegate = notifier
app.run()
