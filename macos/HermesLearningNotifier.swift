import AppKit
import UserNotifications
import Darwin

private struct Receipt: Decodable {
    let summary: String
    let text: String
    let scope: String?

    init(at url: URL) throws {
        self = try JSONDecoder().decode(Self.self, from: Data(contentsOf: url))
    }
}

private final class Notifier: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()
    private let openAction = "view-learning-receipt"
    private let category = "learning-receipt"
    private var window: NSWindow?
    private var reviewTarget: (receipt: URL, scope: String)?
    private var reviewProcesses: [Process] = []

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
        } else if arguments.count == 2 && arguments[0] == "--authorize-result" {
            let url = URL(fileURLWithPath: arguments[1])
            NSApp.setActivationPolicy(.regular)
            NSApp.activate(ignoringOtherApps: true)
            center.requestAuthorization(options: .alert) { allowed, error in
                self.center.getNotificationSettings { settings in
                    let result: [String: Any] = [
                        "allowed": allowed,
                        "authorization_status": settings.authorizationStatus.rawValue,
                        "bundle_identifier": Bundle.main.bundleIdentifier ?? "",
                        "bundle_path": Bundle.main.bundleURL.path,
                        "error": error?.localizedDescription ?? "",
                    ]
                    do {
                        let data = try JSONSerialization.data(withJSONObject: result, options: .sortedKeys)
                        try data.write(to: url, options: .atomic)
                        self.finish(nil)
                    } catch {
                        self.finish(error.localizedDescription)
                    }
                }
            }
        } else if arguments.count == 2 && arguments[0] == "--show-receipt" {
            showReceipt(URL(fileURLWithPath: arguments[1]))
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
                self.center.add(request) { error in
                    if error == nil {
                        try? Data("accepted".utf8).write(to: URL(fileURLWithPath: url.path + ".sent"), options: .atomic)
                    }
                    self.finish(error?.localizedDescription)
                }
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
        let content = NSView(frame: frame)
        let scroll = NSScrollView(frame: NSRect(x: 0, y: 50, width: frame.width, height: frame.height - 50))
        scroll.autoresizingMask = [.width, .height]
        scroll.hasVerticalScroller = true
        let text = NSTextView(frame: scroll.bounds)
        let receipt = try? Receipt(at: url)
        if let scope = receipt?.scope, scope.hasPrefix("/") {
            reviewTarget = (url, scope)
        } else {
            reviewTarget = nil
        }
        text.string = receipt.map { "范围：\($0.scope ?? "本机")\n\n\($0.text)" }
            ?? "这条通知对应的本地回执已无法读取。"
        text.font = .systemFont(ofSize: 14)
        text.isEditable = false
        text.isRichText = false
        text.textContainerInset = NSSize(width: 22, height: 18)
        text.isVerticallyResizable = true
        text.autoresizingMask = .width
        text.textContainer?.widthTracksTextView = true
        scroll.documentView = text
        content.addSubview(scroll)
        let review = NSButton(title: "查看当前待审", target: self, action: #selector(openReviews))
        review.frame = NSRect(x: 22, y: 12, width: 145, height: 28)
        review.bezelStyle = .rounded
        review.isEnabled = reviewTarget != nil
        content.addSubview(review)
        let note = NSTextField(labelWithString: "回执记录生成时的结果；当前状态以审批页为准")
        note.frame = NSRect(x: 182, y: 14, width: frame.width - 200, height: 22)
        note.font = .systemFont(ofSize: 11)
        note.textColor = .secondaryLabelColor
        note.autoresizingMask = .width
        content.addSubview(note)

        let viewer = window ?? NSWindow(contentRect: frame, styleMask: [.titled, .closable, .resizable, .miniaturizable],
                                        backing: .buffered, defer: false)
        viewer.isReleasedWhenClosed = false
        viewer.title = "Hermes 学习明细"
        viewer.contentView = content
        if window == nil { viewer.center() }
        window = viewer
        NSApp.activate(ignoringOtherApps: true)
        viewer.makeKeyAndOrderFront(nil)
        if receipt != nil {
            try? Data("opened".utf8).write(to: URL(fileURLWithPath: url.path + ".read"), options: .atomic)
        }
    }

    @objc private func openReviews() {
        guard let target = reviewTarget else { return }
        let receiver = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/HermesLearningSSH", isDirectory: true)
        let receiptDirectory = target.receipt.resolvingSymlinksInPath().deletingLastPathComponent()
        let process = Process()
        if receiptDirectory.path == receiver.resolvingSymlinksInPath().path {
            let helper = receiver.appendingPathComponent("review.command")
            guard let attributes = try? FileManager.default.attributesOfItem(atPath: helper.path),
                  (attributes[.ownerAccountID] as? NSNumber)?.uint32Value == getuid(),
                  attributes[.type] as? FileAttributeType == .typeRegular,
                  let permissions = (attributes[.posixPermissions] as? NSNumber)?.intValue,
                  permissions & 0o022 == 0, permissions & 0o100 != 0 else {
                let alert = NSAlert()
                alert.messageText = "SSH 审批入口尚未安装"
                alert.informativeText = "请先运行 Mac 验收包中的安装入口。"
                alert.runModal()
                return
            }
            process.executableURL = helper
            process.arguments = [target.scope]
        } else {
            guard let script = Bundle.main.url(forResource: "review", withExtension: "py"),
                  let python = Bundle.main.object(forInfoDictionaryKey: "HermesReviewPython") as? String else {
                let alert = NSAlert()
                alert.messageText = "本机审批入口尚未安装"
                alert.informativeText = "请更新 Hermes 学习通知 App。"
                alert.runModal()
                return
            }
            process.executableURL = URL(fileURLWithPath: python)
            process.arguments = ["-I", "-B", script.path, "--receipt", target.receipt.path]
        }
        process.standardOutput = FileHandle.nullDevice
        let errors = Pipe()
        process.standardError = errors
        process.terminationHandler = { task in
            DispatchQueue.main.async {
                self.reviewProcesses.removeAll { $0 === task }
                if task.terminationStatus != 0 {
                    let alert = NSAlert()
                    alert.messageText = "无法打开审批页"
                    let detail = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
                    alert.informativeText = String(detail.prefix(1200))
                    alert.runModal()
                }
            }
        }
        do {
            try process.run()
            reviewProcesses.append(process)
        } catch {
            let alert = NSAlert()
            alert.messageText = "无法启动审批入口"
            alert.informativeText = error.localizedDescription
            alert.runModal()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

let app = NSApplication.shared
private let notifier = Notifier()
app.delegate = notifier
app.run()
