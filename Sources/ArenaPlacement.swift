import Cocoa

/// Pure geometry: keep the panel beside a window when space permits, otherwise
/// inset it over the game's right edge. Quartz coordinates are converted by caller.
enum ArenaPlacement {
    static func frame(game: NSRect, visible: NSRect, size: NSSize, offset: NSPoint = .zero) -> NSRect {
        let width = min(size.width, max(0, visible.width - 12))
        let height = min(size.height, max(0, visible.height - 12))
        let gap: CGFloat = 12
        var x: CGFloat
        if visible.maxX - game.maxX >= width + gap { x = game.maxX + gap }
        else if game.minX - visible.minX >= width + gap { x = game.minX - width - gap }
        else { x = game.maxX - width - gap }
        let y = game.maxY - height - 28
        x += offset.x
        return NSRect(x: min(max(x, visible.minX + 6), visible.maxX - width - 6),
                      y: min(max(y + offset.y, visible.minY + 6), visible.maxY - height - 6),
                      width: width, height: height)
    }
}
