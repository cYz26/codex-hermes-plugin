import AppKit
import Foundation

private func raster(_ image: NSImage) -> NSBitmapImageRep? {
    guard let bitmap = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 128, pixelsHigh: 128,
                                       bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true,
                                       isPlanar: false, colorSpaceName: .deviceRGB,
                                       bytesPerRow: 0, bitsPerPixel: 0),
          let context = NSGraphicsContext(bitmapImageRep: bitmap) else { return nil }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    image.draw(in: NSRect(x: 0, y: 0, width: 128, height: 128), from: .zero,
               operation: .copy, fraction: 1)
    NSGraphicsContext.restoreGraphicsState()
    return bitmap
}

private func pixels(_ bitmap: NSBitmapImageRep?) -> Data? {
    guard let bitmap, let bytes = bitmap.bitmapData else { return nil }
    return Data(bytes: bytes, count: bitmap.bytesPerRow * bitmap.pixelsHigh)
}

let arguments = Array(CommandLine.arguments.dropFirst())
guard arguments.count == 3 else {
    FileHandle.standardError.write(Data("Expected app path, icon path and report directory\n".utf8))
    exit(2)
}
let appURL = URL(fileURLWithPath: arguments[0]).resolvingSymlinksInPath()
let image = arguments[1].isEmpty ? nil : NSImage(contentsOfFile: arguments[1])
let identifier = Bundle(url: appURL)?.bundleIdentifier ?? "com.cy.hermes-learning-notifier"
let registered = NSWorkspace.shared.urlForApplication(withBundleIdentifier: identifier)?
    .resolvingSymlinksInPath()
let appBitmap = raster(NSWorkspace.shared.icon(forFile: appURL.path))
let genericBitmap = raster(NSWorkspace.shared.icon(forFileType: "app"))
let appPixels = pixels(appBitmap)
let genericPixels = pixels(genericBitmap)
let preview = URL(fileURLWithPath: arguments[2]).appendingPathComponent("macos-app-icon.png")
var result: [String: Any] = [
    "native_icon_decoded": image?.isValid ?? false,
    "native_icon_sizes": image?.representations.map { ["width": $0.pixelsWide, "height": $0.pixelsHigh] } ?? [],
    "default_registered_app": registered?.path ?? "",
    "default_registered_app_matches_sender": registered == appURL,
]
if let appPixels, let genericPixels {
    result["workspace_icon_matches_generic"] = appPixels == genericPixels
}
if let png = appBitmap?.representation(using: .png, properties: [:]) {
    try png.write(to: preview, options: .atomic)
    result["workspace_icon_preview"] = preview.path
}
let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data("\n".utf8))
