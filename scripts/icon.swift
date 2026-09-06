import Cocoa
let output=CommandLine.arguments[1]
let sizes=[16,32,128,256,512]
for size in sizes {
 for scale in [1,2] {
  let n=size*scale
  let bitmap=NSBitmapImageRep(bitmapDataPlanes:nil,pixelsWide:n,pixelsHigh:n,bitsPerSample:8,samplesPerPixel:4,hasAlpha:true,isPlanar:false,colorSpaceName:.deviceRGB,bytesPerRow:0,bitsPerPixel:0)!
  NSGraphicsContext.saveGraphicsState()
  NSGraphicsContext.current=NSGraphicsContext(bitmapImageRep:bitmap)
  let ctx=NSGraphicsContext.current!.cgContext
  ctx.scaleBy(x:CGFloat(n)/1024,y:CGFloat(n)/1024)
  let outer=NSBezierPath(roundedRect:NSRect(x:24,y:24,width:976,height:976),xRadius:218,yRadius:218)
  NSColor(red:0.09,green:0.085,blue:0.14,alpha:1).setFill();outer.fill()
  let gradient=NSGradient(starting:NSColor(red:0.27,green:0.19,blue:0.38,alpha:1),ending:NSColor(red:0.10,green:0.11,blue:0.18,alpha:1))!
  gradient.draw(in:outer,angle:130)
  NSColor(red:0.66,green:0.51,blue:0.8,alpha:0.30).setStroke()
  let ring=NSBezierPath(ovalIn:NSRect(x:150,y:150,width:724,height:724));ring.lineWidth=4;ring.stroke()
  ctx.saveGState();ctx.translateBy(x:512,y:512);ctx.rotate(by: -0.15)
  let card=NSBezierPath(roundedRect:NSRect(x:-217,y:-302,width:434,height:604),xRadius:31,yRadius:31)
  NSColor(red:0.16,green:0.13,blue:0.23,alpha:1).setFill();card.fill()
  NSColor(red:0.79,green:0.67,blue:0.91,alpha:1).setStroke();card.lineWidth=7;card.stroke()
  let inner=NSBezierPath(roundedRect:NSRect(x:-194,y:-279,width:388,height:558),xRadius:20,yRadius:20);inner.lineWidth=2;inner.stroke()
  let star=NSBezierPath();star.move(to:NSPoint(x:0,y:200));star.line(to:NSPoint(x:48,y:55));star.line(to:NSPoint(x:151,y:0));star.line(to:NSPoint(x:48,y:-55));star.line(to:NSPoint(x:0,y:-200));star.line(to:NSPoint(x:-48,y:-55));star.line(to:NSPoint(x:-151,y:0));star.line(to:NSPoint(x:-48,y:55));star.close()
  NSColor(red:0.9,green:0.77,blue:0.54,alpha:1).setFill();star.fill()
  ctx.restoreGState()
  NSGraphicsContext.restoreGraphicsState()
  try bitmap.representation(using:.png,properties:[:])!.write(to:URL(fileURLWithPath:output+"/icon_\(size)x\(size)\(scale==2 ? "@2x":"").png"))
 }
}
