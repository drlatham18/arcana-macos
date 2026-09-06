import Cocoa

@main
struct PlacementTests {
    static func main() {
        let screen = NSRect(x:0,y:0,width:1920,height:1080)
        let size = NSSize(width:360,height:520)
        func check(_ game:NSRect, _ visible:NSRect=screen, _ offset:NSPoint = .zero) -> NSRect {
            let result=ArenaPlacement.frame(game:game,visible:visible,size:size,offset:offset)
            precondition(visible.contains(result), "Panel must fit the selected screen: \(result)")
            return result
        }
        let right=check(NSRect(x:100,y:100,width:1000,height:800))
        precondition(right.minX==1112, "Use free space to the right")
        let left=check(NSRect(x:600,y:100,width:1250,height:800))
        precondition(left.maxX==588, "Use free space to the left")
        let full=check(screen)
        precondition(full.maxX==1908, "Inset on the right for fullscreen")
        let secondary=NSRect(x:-1600,y:200,width:1600,height:900)
        let second=check(secondary,secondary)
        precondition(second.maxX == -12, "Honor negative monitor coordinates")
        let shifted=check(screen,screen,NSPoint(x:-100,y:-50))
        precondition(shifted.minX==full.minX-100 && shifted.minY==full.minY-50, "Preserve dragged offset")
        _=check(screen,screen,NSPoint(x:10000,y:-10000))
        _=check(NSRect(x:0,y:0,width:320,height:300),NSRect(x:0,y:0,width:320,height:300))
        print("7 placement checks passed")
    }
}
