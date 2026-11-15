import AppKit
import UserNotifications
import Darwin

private struct ReceiptPresentation: Decodable {
    struct Section: Decodable {
        let title: String
        let text: String
    }

    let title: String
    let subtitle: String
    let body: String
    let tone: String
    let sections: [Section]
    let retryAt: Double?

    private enum CodingKeys: String, CodingKey {
        case title, subtitle, body, tone, sections
        case retryAt = "retry_at"
    }

    var retrySchedule: String? {
        guard let retryAt, retryAt.isFinite, (0..<253402300800).contains(retryAt) else { return nil }
        let date = Date(timeIntervalSince1970: retryAt)
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.dateFormat = Calendar.current.isDateInToday(date) ? "HH:mm" : "M月d日 HH:mm"
        return (date < Date() ? "原定 " : "预计 ") + formatter.string(from: date) + " 自动重试"
    }

    var bannerTitle: String { (tone == "waiting" ? retrySchedule : nil) ?? title }

    var bannerBody: String {
        guard tone != "waiting", let retrySchedule else { return body }
        return [retrySchedule, body].filter { !$0.isEmpty }.joined(separator: " · ")
    }

    var badge: (symbol: String, color: NSColor) {
        switch tone {
        case "success": return ("checkmark.circle.fill", .systemGreen)
        case "waiting": return ("clock.fill", .systemBlue)
        case "warning": return ("exclamationmark.triangle.fill", .systemOrange)
        case "error": return ("xmark.octagon.fill", .systemRed)
        default: return ("info.circle.fill", .systemBlue)
        }
    }
}

private struct ReceiptEntry: Decodable {
    let summary: String
    let text: String
    let presentation: ReceiptPresentation?

    private enum CodingKeys: String, CodingKey { case summary, text, presentation }

    init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        summary = try fields.decode(String.self, forKey: .summary)
        text = try fields.decode(String.self, forKey: .text)
        presentation = try? fields.decode(ReceiptPresentation.self, forKey: .presentation)
    }
}

private struct Receipt: Decodable {
    let entry: ReceiptEntry
    let entries: [ReceiptEntry]
    let scope: String?
    let kind: String?
    let notificationScope: String?
    let projectApproval: Bool?
    let approvalSubject: String?

    private enum CodingKeys: String, CodingKey {
        case entries, scope, kind
        case notificationScope = "notification_scope"
        case projectApproval = "project_approval"
        case approvalSubject = "approval_subject"
    }

    var requiresApproval: Bool { kind == "approval.required" }
    var isPublicationReview: Bool { requiresApproval && approvalSubject == "publication" }
    var isGlobal: Bool {
        isPublicationReview || notificationScope == "global" || kind == "publication.applied"
            || kind == "publication.pending" || (notificationScope == nil && scope == "global")
    }
    var bannerTitle: String {
        if let presentation = entry.presentation {
            let title = presentation.bannerTitle
            let symbol: String
            switch presentation.tone {
            case "success": symbol = "✅"
            case "waiting": symbol = "🕒"
            case "warning": symbol = "⚠️"
            case "error": symbol = "❌"
            default: return title
            }
            return title.hasPrefix(symbol) ? title : "\(symbol) \(title)"
        }
        if requiresApproval {
            return isPublicationReview ? "⚠️ 全局待审批" : "⚠️ 项目待审批"
        }
        if isGlobal { return kind == "publication.pending" ? "共享候选待检查" : "全局共享回执" }
        return notificationScope == "project" ? "项目学习回执" : "Hermes 学习明细"
    }
    var threadIdentifier: String {
        let group = isGlobal ? "global" : notificationScope == "system" ? "system" : "project"
        return "hermes.\(group).\(requiresApproval ? "approval" : "receipt")"
    }

    var reviewScope: String? {
        guard let scope, scope.hasPrefix("/"), projectApproval != false else { return nil }
        return scope
    }

    init(at url: URL) throws {
        self = try JSONDecoder().decode(Self.self, from: Data(contentsOf: url))
    }

    init(from decoder: Decoder) throws {
        let fields = try decoder.container(keyedBy: CodingKeys.self)
        entry = try ReceiptEntry(from: decoder)
        entries = (try? fields.decode([ReceiptEntry].self, forKey: .entries)) ?? []
        scope = try fields.decodeIfPresent(String.self, forKey: .scope)
        kind = try fields.decodeIfPresent(String.self, forKey: .kind)
        notificationScope = try fields.decodeIfPresent(String.self, forKey: .notificationScope)
        projectApproval = try fields.decodeIfPresent(Bool.self, forKey: .projectApproval)
        approvalSubject = try fields.decodeIfPresent(String.self, forKey: .approvalSubject)
    }
}

private final class ReceiptDocument: NSView {
    override var isFlipped: Bool { true }
}

private func receiptStack(spacing: CGFloat) -> NSStackView {
    let stack = NSStackView()
    stack.orientation = .vertical
    stack.alignment = .leading
    stack.spacing = spacing
    stack.translatesAutoresizingMaskIntoConstraints = false
    return stack
}

private func appendFullWidth(_ view: NSView, to stack: NSStackView) {
    stack.addArrangedSubview(view)
    view.widthAnchor.constraint(equalTo: stack.widthAnchor).isActive = true
}

private func receiptLabel(_ text: String, font: NSFont = .systemFont(ofSize: 14),
                          color: NSColor = .labelColor) -> NSTextField {
    let label = NSTextField(wrappingLabelWithString: text)
    label.font = font
    label.textColor = color
    label.isSelectable = true
    label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    return label
}

private final class ReceiptDisclosure: NSStackView {
    private let button: NSButton
    private let makeContent: () -> NSView
    private var expandedContent: NSView?

    init(_ title: String, content: @escaping () -> NSView) {
        button = NSButton(title: title, target: nil, action: nil)
        makeContent = content
        super.init(frame: .zero)
        orientation = .vertical
        alignment = .leading
        spacing = 8
        button.bezelStyle = .recessed
        button.imagePosition = .imageLeading
        button.target = self
        button.action = #selector(toggleContent)
        button.toolTip = title
        button.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        addArrangedSubview(button)
        button.widthAnchor.constraint(lessThanOrEqualTo: widthAnchor).isActive = true
        updateButton(expanded: false)
    }

    required init?(coder: NSCoder) { fatalError("ReceiptDisclosure is created in code") }

    private func updateButton(expanded: Bool) {
        button.image = NSImage(systemSymbolName: expanded ? "chevron.down" : "chevron.right", accessibilityDescription: nil)
        button.setAccessibilityExpanded(expanded)
    }

    @objc private func toggleContent() {
        if let expandedContent {
            removeArrangedSubview(expandedContent)
            expandedContent.removeFromSuperview()
            self.expandedContent = nil
        } else {
            let content = makeContent()
            appendFullWidth(content, to: self)
            expandedContent = content
        }
        updateButton(expanded: expandedContent != nil)
    }
}

private func appendPresentation(_ presentation: ReceiptPresentation, to stack: NSStackView) {
    let icon = NSImageView()
    icon.image = NSImage(systemSymbolName: presentation.badge.symbol, accessibilityDescription: nil)
    icon.symbolConfiguration = .init(pointSize: 22, weight: .semibold)
    icon.contentTintColor = presentation.badge.color
    icon.widthAnchor.constraint(equalToConstant: 25).isActive = true
    icon.heightAnchor.constraint(equalToConstant: 26).isActive = true
    let header = NSStackView(views: [icon, receiptLabel(presentation.bannerTitle, font: .systemFont(ofSize: 22, weight: .semibold))])
    header.spacing = 10
    header.alignment = .centerY
    appendFullWidth(header, to: stack)
}

private func appendEntry(_ entry: ReceiptEntry, to stack: NSStackView, showHeading: Bool) {
    guard let presentation = entry.presentation else {
        if showHeading, !entry.summary.isEmpty {
            appendFullWidth(receiptLabel(entry.summary, font: .systemFont(ofSize: 17, weight: .semibold)), to: stack)
        }
        appendFullWidth(receiptLabel(entry.text), to: stack)
        return
    }
    if showHeading { appendPresentation(presentation, to: stack) }
    if presentation.sections.isEmpty, !presentation.bannerBody.isEmpty {
        appendFullWidth(receiptLabel(presentation.bannerBody), to: stack)
    }
    for section in presentation.sections {
        let detail = receiptStack(spacing: 5)
        appendFullWidth(receiptLabel(section.title, font: .systemFont(ofSize: 15, weight: .semibold)), to: detail)
        let retry = section.title == "异常与重试" && presentation.tone != "waiting" ? presentation.retrySchedule : nil
        let text = [retry, section.text].compactMap { $0 }.joined(separator: "\n")
        appendFullWidth(receiptLabel(text), to: detail)
        appendFullWidth(detail, to: stack)
    }
    appendFullWidth(ReceiptDisclosure("完整执行记录") {
        receiptLabel(entry.text, font: .monospacedSystemFont(ofSize: 12, weight: .regular))
    }, to: stack)
}

private final class Notifier: NSObject, NSApplicationDelegate, UNUserNotificationCenterDelegate {
    private let center = UNUserNotificationCenter.current()
    private let openAction = "view-learning-receipt"
    private let category = "learning-receipt"
    private let globalCategory = "global-learning-receipt"
    private let globalOpenAction = "view-global-receipt"
    private let reviewAction = "open-pending-reviews"
    private let approvalCategory = "learning-approval"
    private let projectReviewAction = "open-project-approval"
    private let projectApprovalCategory = "project-approval"
    private let publicationReviewAction = "open-publication-approval"
    private let publicationApprovalCategory = "publication-approval"
    private let nativeReviewAction = "view-native-approval-instructions"
    private let nativeApprovalCategory = "native-approval"
    private var window: NSWindow?
    private var reviewTarget: (receipt: URL, scope: String)?
    private var reviewProcesses: [Process] = []

    func applicationWillFinishLaunching(_ notification: Notification) {
        center.delegate = self
        let view = UNNotificationAction(identifier: openAction, title: "查看学习明细", options: .foreground)
        let shared = UNNotificationAction(identifier: globalOpenAction, title: "查看共享明细", options: .foreground,
                                          icon: UNNotificationActionIcon(systemImageName: "globe"))
        let review = UNNotificationAction(identifier: reviewAction, title: "查看当前待审", options: .foreground)
        let project = UNNotificationAction(identifier: projectReviewAction, title: "查看项目审批", options: .foreground,
                                           icon: UNNotificationActionIcon(systemImageName: "folder"))
        let publication = UNNotificationAction(identifier: publicationReviewAction, title: "查看共享审批", options: .foreground,
                                               icon: UNNotificationActionIcon(systemImageName: "globe"))
        let native = UNNotificationAction(identifier: nativeReviewAction, title: "查看待审说明", options: .foreground,
                                          icon: UNNotificationActionIcon(systemImageName: "exclamationmark.triangle"))
        center.setNotificationCategories([
            UNNotificationCategory(identifier: category, actions: [view], intentIdentifiers: []),
            UNNotificationCategory(identifier: globalCategory, actions: [shared], intentIdentifiers: []),
            UNNotificationCategory(identifier: approvalCategory, actions: [review], intentIdentifiers: []),
            UNNotificationCategory(identifier: projectApprovalCategory, actions: [project], intentIdentifiers: []),
            UNNotificationCategory(identifier: publicationApprovalCategory, actions: [publication], intentIdentifiers: []),
            UNNotificationCategory(identifier: nativeApprovalCategory, actions: [native], intentIdentifiers: []),
        ])
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let arguments = Array(CommandLine.arguments.dropFirst())
        if arguments == ["--authorize"] {
            center.requestAuthorization(options: [.alert, .sound]) { allowed, error in
                self.finish(error?.localizedDescription ?? (allowed ? nil : "已关闭 Hermes 通知"))
            }
        } else if arguments.count == 2 && arguments[0] == "--authorize-result" {
            let url = URL(fileURLWithPath: arguments[1])
            NSApp.setActivationPolicy(.regular)
            NSApp.activate(ignoringOtherApps: true)
            center.requestAuthorization(options: [.alert, .sound]) { allowed, error in
                self.center.getNotificationSettings { settings in
                    let result: [String: Any] = [
                        "allowed": allowed,
                        "authorization_status": settings.authorizationStatus.rawValue,
                        "sound_setting": settings.soundSetting.rawValue,
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
                    let name = Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String ?? "Hermes 学习"
                    self.finish("请在系统设置的通知中允许「\(name)」发送通知")
                    return
                }
                let content = UNMutableNotificationContent()
                content.title = receipt.bannerTitle
                content.subtitle = receipt.entry.presentation?.subtitle ?? ""
                content.body = receipt.entry.presentation?.bannerBody ?? receipt.entry.summary
                content.categoryIdentifier = self.category(for: receipt)
                content.threadIdentifier = receipt.threadIdentifier
                if receipt.requiresApproval {
                    if settings.soundSetting == .enabled { content.sound = .default }
                    if let icon = Bundle.main.url(forResource: "hermes-approval-indicator", withExtension: "png"),
                       let attachment = try? UNNotificationAttachment(identifier: "approval", url: icon) {
                        content.attachments = [attachment]
                    }
                }
                content.userInfo = ["receiptPath": url.path]
                let trigger = UNTimeIntervalNotificationTrigger(timeInterval: 1, repeats: false)
                let request = UNNotificationRequest(identifier: url.deletingPathExtension().lastPathComponent,
                                                    content: content, trigger: trigger)
                self.submit(request, receipt: url)
            }
        } catch {
            finish(error.localizedDescription)
        }
    }

    private func submit(_ request: UNNotificationRequest, receipt: URL) {
        center.add(request) { error in
            if let error = error as NSError?, error.domain == UNErrorDomain,
               (100...105).contains(error.code), !request.content.attachments.isEmpty,
               let content = request.content.mutableCopy() as? UNMutableNotificationContent {
                content.attachments = []
                self.submit(UNNotificationRequest(identifier: request.identifier, content: content,
                                                  trigger: request.trigger), receipt: receipt)
                return
            }
            if error == nil {
                try? Data("accepted".utf8).write(to: URL(fileURLWithPath: receipt.path + ".sent"), options: .atomic)
            }
            self.finish(error?.localizedDescription)
        }
    }

    private func category(for receipt: Receipt) -> String {
        guard receipt.requiresApproval else { return receipt.isGlobal ? globalCategory : category }
        guard receipt.reviewScope != nil else { return nativeApprovalCategory }
        return receipt.isPublicationReview ? publicationApprovalCategory : projectApprovalCategory
    }

    private func finish(_ error: String?) {
        if let error {
            FileHandle.standardError.write(Data((error + "\n").utf8))
        }
        exit(error == nil ? 0 : 1)
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        let action = response.actionIdentifier
        guard let path = response.notification.request.content.userInfo["receiptPath"] as? String else {
            completionHandler()
            return
        }
        DispatchQueue.main.async {
            let url = URL(fileURLWithPath: path)
            let approvalActions = [self.reviewAction, self.projectReviewAction, self.publicationReviewAction]
            let openReview = approvalActions.contains(action) || action == UNNotificationDefaultActionIdentifier
            if openReview, let receipt = try? Receipt(at: url), receipt.requiresApproval,
               let scope = receipt.reviewScope {
                self.openReviews(receipt: url, scope: scope)
            } else if openReview || [self.openAction, self.globalOpenAction, self.nativeReviewAction].contains(action) {
                self.showReceipt(url)
            }
            completionHandler()
        }
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler(notification.request.content.sound == nil ? [.banner, .list] : [.banner, .list, .sound])
    }

    private func showReceipt(_ url: URL) {
        let receipt = try? Receipt(at: url)
        reviewTarget = receipt?.reviewScope.map { (url, $0) }
        let frame = NSRect(x: 0, y: 0, width: 660, height: 510)
        let content = NSView(frame: frame)
        let scroll = NSScrollView()
        scroll.translatesAutoresizingMaskIntoConstraints = false
        scroll.hasVerticalScroller = true
        scroll.drawsBackground = false
        let document = ReceiptDocument()
        document.translatesAutoresizingMaskIntoConstraints = false
        let stack = receiptStack(spacing: 16)
        document.addSubview(stack)
        scroll.documentView = document
        content.addSubview(scroll)
        NSLayoutConstraint.activate([
            scroll.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            scroll.topAnchor.constraint(equalTo: content.topAnchor),
            document.leadingAnchor.constraint(equalTo: scroll.contentView.leadingAnchor),
            document.trailingAnchor.constraint(equalTo: scroll.contentView.trailingAnchor),
            document.topAnchor.constraint(equalTo: scroll.contentView.topAnchor),
            stack.leadingAnchor.constraint(equalTo: document.leadingAnchor, constant: 22),
            stack.trailingAnchor.constraint(equalTo: document.trailingAnchor, constant: -22),
            stack.topAnchor.constraint(equalTo: document.topAnchor, constant: 20),
            stack.bottomAnchor.constraint(equalTo: document.bottomAnchor, constant: -20),
        ])

        if let receipt {
            if let presentation = receipt.entry.presentation { appendPresentation(presentation, to: stack) }
            var scope = receipt.isGlobal ? "跨项目共享" : receipt.scope == "system" ? "系统通知" : "项目学习"
            if let source = receipt.scope, source.hasPrefix("/") { scope += " · " + source }
            appendFullWidth(receiptLabel(scope, font: .systemFont(ofSize: 12), color: .secondaryLabelColor), to: stack)
            if receipt.entries.isEmpty {
                appendEntry(receipt.entry, to: stack, showHeading: receipt.entry.presentation == nil)
            } else {
                if let body = receipt.entry.presentation?.bannerBody, !body.isEmpty {
                    appendFullWidth(receiptLabel(body), to: stack)
                }
                for entry in receipt.entries {
                    let presentation = entry.presentation
                    let title = presentation.map { [$0.bannerTitle, $0.body].filter { !$0.isEmpty }.joined(separator: " · ") }
                        ?? String(entry.summary.prefix { !$0.isNewline })
                    appendFullWidth(ReceiptDisclosure(title.isEmpty ? "查看这条回执" : title) {
                        let detail = receiptStack(spacing: 12)
                        appendEntry(entry, to: detail, showHeading: false)
                        return detail
                    }, to: stack)
                }
            }
        } else {
            appendFullWidth(receiptLabel("这条通知对应的本地回执已无法读取。"), to: stack)
        }

        if reviewTarget != nil {
            let review = NSButton(title: receipt?.isPublicationReview == true ? "查看共享审批" : "查看项目审批",
                                  target: self, action: #selector(openCurrentReviews))
            review.bezelStyle = .rounded
            let note = receiptLabel("审批页会显示当前待审状态。", font: .systemFont(ofSize: 11), color: .secondaryLabelColor)
            let footer = NSStackView(views: [review, note])
            footer.alignment = .centerY
            footer.spacing = 12
            footer.translatesAutoresizingMaskIntoConstraints = false
            content.addSubview(footer)
            NSLayoutConstraint.activate([
                footer.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 22),
                footer.trailingAnchor.constraint(lessThanOrEqualTo: content.trailingAnchor, constant: -22),
                footer.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -11),
                scroll.bottomAnchor.constraint(equalTo: footer.topAnchor, constant: -4),
            ])
        } else {
            scroll.bottomAnchor.constraint(equalTo: content.bottomAnchor).isActive = true
        }

        let viewer = window ?? NSWindow(contentRect: frame, styleMask: [.titled, .closable, .resizable, .miniaturizable],
                                        backing: .buffered, defer: false)
        viewer.isReleasedWhenClosed = false
        viewer.minSize = NSSize(width: 430, height: 300)
        viewer.title = receipt?.isGlobal == true ? "Hermes 全局共享明细" : "Hermes 学习明细"
        viewer.contentView = content
        if window == nil { viewer.center() }
        window = viewer
        NSApp.activate(ignoringOtherApps: true)
        viewer.makeKeyAndOrderFront(nil)
        if receipt != nil { markOpened(url) }
    }

    private func markOpened(_ url: URL) {
        try? Data("opened".utf8).write(to: URL(fileURLWithPath: url.path + ".read"), options: .atomic)
    }

    @objc private func openCurrentReviews() {
        guard let target = reviewTarget else { return }
        openReviews(receipt: target.receipt, scope: target.scope)
    }

    private func openReviews(receipt: URL, scope: String) {
        let receiver = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/HermesLearningSSH", isDirectory: true)
        let receiptDirectory = receipt.resolvingSymlinksInPath().deletingLastPathComponent()
        let remote = receiptDirectory.path == receiver.resolvingSymlinksInPath().path
        let process = Process()
        if remote {
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
            let publication = (try? Receipt(at: receipt))?.isPublicationReview == true
            process.arguments = publication ? [scope, "--publication"] : [scope]
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
            process.arguments = ["-I", "-B", script.path, "--receipt", receipt.path]
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
                } else if remote {
                    self.markOpened(receipt)
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
