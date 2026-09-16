import Cocoa
import WebKit
import UniformTypeIdentifiers

final class CompanionPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKScriptMessageHandler, WKNavigationDelegate {
    var window: NSWindow!
    var mainWindow: NSWindow!
    var companion: CompanionPanel?
    var followTimer: Timer?
    var workspaceObservers = [NSObjectProtocol]()
    var followsArena = UserDefaults.standard.object(forKey: "FollowArena") as? Bool ?? true
    var panelDismissed = false
    var applyingPlacement = false
    var placementOffset = NSPoint.zero
    var lastGameRect: NSRect?
    var lastBaseFrame: NSRect?
    var lastFollowing = false
    var web: WKWebView!
    var engine: Process?
    var input: Pipe?
    var output: Pipe?
    var buffer = Data()
    let io = DispatchQueue(label: "app.arcana.engine")
    var statusItem: NSStatusItem!
    var ready = false
    var compactMode = false
    var queued = [String]()
    var shuttingDown = false
    let resources = Bundle.main.resourceURL!

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let config = WKWebViewConfiguration()
        config.userContentController.add(self, name: "bridge")
        web = WKWebView(frame: .zero, configuration: config)
        web.navigationDelegate = self
        web.setValue(false, forKey: "drawsBackground")
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1220, height: 810), styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        mainWindow = window
        window.title = "Arcana"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.backgroundColor = NSColor(red:0.047,green:0.055,blue:0.078,alpha:1)
        window.minSize = NSSize(width: 920,height: 650)
        window.contentView = web
        window.isReleasedWhenClosed = false
        window.setFrameAutosaveName("ArcanaMainWindow")
        window.center()
        buildMenu()
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(systemSymbolName:"sparkles",accessibilityDescription:"Arcana")
        statusItem.button?.target = self
        statusItem.button?.action = #selector(showWindow)
        startEngine()
        web.loadFileURL(resources.appendingPathComponent("web/index.html"), allowingReadAccessTo: resources.appendingPathComponent("web"))
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps:true)
        installArenaFollowing()
        if UserDefaults.standard.bool(forKey: "CompactMode") {
            setCompact(true)
        }
    }
    func buildMenu() {
        let menu = NSMenu()
        let app = NSMenuItem(); menu.addItem(app)
        let appMenu = NSMenu(); app.submenu = appMenu
        appMenu.addItem(withTitle:"About Arcana",action:#selector(about),keyEquivalent:"").target = self
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle:"Hide Arcana",action:#selector(NSApplication.hide(_:)),keyEquivalent:"h")
        appMenu.addItem(withTitle:"Quit Arcana",action:#selector(NSApplication.terminate(_:)),keyEquivalent:"q")
        let edit = NSMenuItem(); menu.addItem(edit); edit.submenu = NSMenu(title:"Edit")
        for (title,action,key) in [("Undo",Selector(("undo:")),"z"),("Cut",#selector(NSText.cut(_:)),"x"),("Copy",#selector(NSText.copy(_:)),"c"),("Paste",#selector(NSText.paste(_:)),"v"),("Select All",#selector(NSText.selectAll(_:)),"a")] { edit.submenu?.addItem(withTitle:title,action:action,keyEquivalent:key) }
        let view = NSMenuItem();menu.addItem(view);view.submenu = NSMenu(title:"View")
        let show = view.submenu!.addItem(withTitle:"Show Arcana",action:#selector(showWindow),keyEquivalent:"0");show.target = self
        let compact = view.submenu!.addItem(withTitle:"Float Above Windows",action:#selector(toggleFloat),keyEquivalent:"f"); compact.target=self;compact.keyEquivalentModifierMask = [.command,.shift]
        let mini = view.submenu!.addItem(withTitle:"Toggle Compact Companion",action:#selector(toggleCompact),keyEquivalent:"m")
        mini.target=self; mini.keyEquivalentModifierMask=[.command,.shift]
        NSApp.mainMenu = menu
    }
    @objc func about() {
        let alert=NSAlert();alert.messageText="Arcana 1.2";alert.informativeText="Your offline MTG Arena companion for macOS.\n\nLive board • Rules library • Match reviews\nBuilt from your MTGA / MTGO Rules Companion.\n\nUnofficial fan software; not affiliated with Wizards of the Coast."
        alert.runModal()
    }
    @objc func showWindow() {
        panelDismissed = false
        if compactMode { window.orderFrontRegardless(); followArena(force:true) }
        else { window.makeKeyAndOrderFront(nil); NSApp.activate(ignoringOtherApps:true) }
    }
    @objc func toggleCompact() { setCompact(!compactMode) }
    func setCompact(_ compact: Bool) {
        guard compact != compactMode else { return }
        compactMode = compact
        UserDefaults.standard.set(compact,forKey:"CompactMode")
        panelDismissed = false
        if compact {
            if companion == nil {
                let panel = CompanionPanel(contentRect:NSRect(x:mainWindow.frame.maxX-360,y:mainWindow.frame.maxY-520,width:360,height:520), styleMask:[.titled,.closable,.resizable,.fullSizeContentView,.nonactivatingPanel],backing:.buffered,defer:false)
                panel.title="Arcana";panel.titleVisibility = .hidden
                panel.titlebarAppearsTransparent=true
                panel.backgroundColor=mainWindow.backgroundColor
                panel.minSize=NSSize(width:300,height:280)
                panel.isReleasedWhenClosed=false
                panel.hidesOnDeactivate=false
                panel.isFloatingPanel=true
                panel.becomesKeyOnlyIfNeeded=true
                panel.collectionBehavior=[.canJoinAllSpaces,.fullScreenAuxiliary,.canJoinAllApplications,.ignoresCycle]
                panel.delegate=self
                companion=panel
            }
            web.removeFromSuperview()
            mainWindow.orderOut(nil)
            window=companion!
            window.contentView=web
            window.level = .floating
            window.orderFrontRegardless()
            followArena(force:true)
        } else {
            web.removeFromSuperview()
            companion?.orderOut(nil)
            window=mainWindow
            window.contentView=web
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps:true)
        }
        deliver("{\"event\":\"compact\",\"enabled\":\(compactMode)}")
        sendFollowStatus(lastFollowing)
    }
    @objc func toggleFloat() {
        window.level = window.level == .floating ? .normal : .floating
        if window.level == .normal && followsArena { setFollowing(false) }
    }
    func setFollowing(_ value:Bool) {
        followsArena=value
        UserDefaults.standard.set(value,forKey:"FollowArena")
        if value { panelDismissed=false; window.level = .floating; followArena(force:true) }
        sendFollowStatus(value && lastFollowing)
    }
    func sendFollowStatus(_ following:Bool) {
        lastFollowing=following
        deliver("{\"event\":\"following\",\"enabled\":\(followsArena),\"attached\":\(following),\"floating\":\(window.level == .floating)}")
    }
    func installArenaFollowing() {
        let center=NSWorkspace.shared.notificationCenter
        workspaceObservers.append(center.addObserver(forName:NSWorkspace.didActivateApplicationNotification,object:nil,queue:.main) { [weak self] note in
            guard let self=self,let app=note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication,app.bundleIdentifier=="com.wizards.mtga",self.followsArena,!self.panelDismissed else{return}
            if !self.compactMode {self.setCompact(true)}
            self.followArena(force:true)
            DispatchQueue.main.asyncAfter(deadline:.now()+0.4) { [weak self] in self?.followArena(force:true) }
        })
        workspaceObservers.append(center.addObserver(forName:NSWorkspace.activeSpaceDidChangeNotification,object:nil,queue:.main) { [weak self] _ in
            DispatchQueue.main.asyncAfter(deadline:.now()+0.4) { [weak self] in self?.followArena(force:true) }
        })
        followTimer=Timer.scheduledTimer(withTimeInterval:0.75,repeats:true) { [weak self] _ in self?.followArena() }
    }
    func followArena(force:Bool=false) {
        guard followsArena,compactMode,!panelDismissed,let panel=companion else{return}
        let front=NSWorkspace.shared.frontmostApplication
        let arenaFront=front?.bundleIdentifier=="com.wizards.mtga"
        // A manual reopen can attach to an already-visible Arena; background
        // polling never moves the panel while someone is working in another app.
        guard arenaFront || (force && front?.processIdentifier==ProcessInfo.processInfo.processIdentifier) else{return}
        guard let arena=NSRunningApplication.runningApplications(withBundleIdentifier:"com.wizards.mtga").first,
              let info=CGWindowListCopyWindowInfo([.optionOnScreenOnly,.excludeDesktopElements],kCGNullWindowID) as? [[String:Any]],
              let primary=NSScreen.screens.first else{sendFollowStatus(false);return}
        // Only public window geometry is inspected. No pixels, titles, or
        // accessibility controls are captured, and Arena is never moved.
        let frames=info.compactMap { row -> NSRect? in
            guard (row[kCGWindowOwnerPID as String] as? NSNumber)?.int32Value==arena.processIdentifier,
                  (row[kCGWindowLayer as String] as? NSNumber)?.intValue==0,
                  let bounds=row[kCGWindowBounds as String] as? NSDictionary,
                  let rect=CGRect(dictionaryRepresentation:bounds),rect.width>300,rect.height>200 else{return nil}
            return NSRect(x:rect.minX,y:primary.frame.maxY-rect.maxY,width:rect.width,height:rect.height)
        }
        guard let game=frames.max(by:{$0.width*$0.height < $1.width*$1.height}),
              let screen=NSScreen.screens.max(by:{intersectionArea($0.frame,game)<intersectionArea($1.frame,game)}) else{sendFollowStatus(false);return}
        if panel.inLiveResize || NSEvent.pressedMouseButtons != 0 {return}
        let base=ArenaPlacement.frame(game:game,visible:screen.visibleFrame,size:panel.frame.size)
        let destination=ArenaPlacement.frame(game:game,visible:screen.visibleFrame,size:panel.frame.size,offset:placementOffset)
        if force || lastGameRect != game || !screen.frame.intersects(panel.frame) {
            applyingPlacement=true
            panel.setFrame(destination,display:true)
            applyingPlacement=false
            lastGameRect=game
        }
        lastBaseFrame=base
        panel.level = .floating
        // Reorder without activating Arcana or making it the key window.
        panel.orderFrontRegardless()
        if !lastFollowing {sendFollowStatus(true)}
    }
    func intersectionArea(_ a:NSRect,_ b:NSRect)->CGFloat {
        let r=a.intersection(b);return r.isNull ? 0:r.width*r.height
    }
    func windowDidMove(_ notification:Notification) {
        guard compactMode,!applyingPlacement,let panel=companion,let base=lastBaseFrame,
              notification.object as? NSWindow === panel else{return}
        placementOffset=NSPoint(x:panel.frame.minX-base.minX,y:panel.frame.minY-base.minY)
    }
    func windowWillClose(_ notification:Notification) {
        if notification.object as? NSWindow === companion {panelDismissed=true;lastFollowing=false}
    }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool { showWindow();return true }
    func applicationWillTerminate(_ notification: Notification) {
        shuttingDown = true
        followTimer?.invalidate()
        workspaceObservers.forEach {NSWorkspace.shared.notificationCenter.removeObserver($0)}
        try? input?.fileHandleForWriting.close()
        if let engine=engine, engine.isRunning { engine.terminate() }
    }
    func startEngine() {
        let process=Process(), stdinPipe=Pipe(), stdoutPipe=Pipe()
        #if arch(arm64)
        let arch="arm64"
        #else
        let arch="x86_64"
        #endif
        process.executableURL = resources.appendingPathComponent("runtime/\(arch)/python/bin/python3")
        process.arguments = ["-I","-B",resources.appendingPathComponent("engine/launch.py").path]
        process.currentDirectoryURL = resources.appendingPathComponent("engine")
        var env=ProcessInfo.processInfo.environment
        for key in Array(env.keys) where key.hasPrefix("PYTHON") || key.hasPrefix("MTGA_") { env.removeValue(forKey:key) }
        env["PYTHONUNBUFFERED"]="1"
        process.environment=env
        process.standardInput=stdinPipe;process.standardOutput=stdoutPipe;process.standardError=FileHandle.nullDevice
        engine=process;input=stdinPipe;output=stdoutPipe
        stdoutPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data=handle.availableData
            guard !data.isEmpty else { handle.readabilityHandler=nil;return }
            self?.io.async { [weak self] in
                guard let self=self else{return};self.buffer.append(data)
                while let end=self.buffer.firstIndex(of:10) {
                    let line=Data(self.buffer[..<end]);self.buffer.removeSubrange(...end)
                    if let text=String(data:line,encoding:.utf8) { DispatchQueue.main.async { self.deliver(text) } }
                }
            }
        }
        process.terminationHandler = { [weak self] _ in DispatchQueue.main.async {
            guard let self=self, !self.shuttingDown else{return}
            self.deliver("{\"event\":\"engineError\",\"error\":\"The companion engine stopped. Quit and reopen Arcana to reconnect.\"}")
        }}
        do { try process.run() } catch { deliver("{\"event\":\"engineError\",\"error\":\"Arcana could not start its bundled engine. Reinstall the complete app.\"}") }
    }
    func deliver(_ json:String) {
        guard ready else { queued.append(json);return }
        // The argument is serialized JSON, never executable text from a log or question.
        web.evaluateJavaScript("window.receive(\(json))",completionHandler:nil)
    }
    func reply(_ id:Any?,result:Any?=nil,error:String?=nil) {
        var object:[String:Any] = ["id":id ?? NSNull(),"ok":error == nil]
        if let result=result { object["result"]=result }
        if let error=error { object["error"]=error }
        if let data=try? JSONSerialization.data(withJSONObject:object),let text=String(data:data,encoding:.utf8) {deliver(text)}
    }
    func userContentController(_ userContentController: WKUserContentController,didReceive message:WKScriptMessage) {
        guard message.frameInfo.isMainFrame,let body=message.body as? [String:Any],let action=body["action"] as? String else{return}
        let id=body["id"]
        if action=="ready" { ready=true;let pending=queued;queued=[];pending.forEach{deliver($0)};return }
        if action=="choose_log" || action=="choose_database" || action=="choose_rules" {
            let panel=NSOpenPanel();panel.canChooseDirectories=false;panel.allowsMultipleSelection=false
            panel.message=action=="choose_rules" ? "Choose your downloaded Wizards Comprehensive Rules .txt file. Arcana indexes it locally." : action=="choose_log" ? "Choose Arena’s Player.log. Arcana reads it without changing it." : "Choose Arena’s Raw_CardDatabase_….mtga file."
            panel.beginSheetModal(for:window) { response in
                guard response == .OK,let url=panel.url else {self.reply(id,result:["cancelled":true]);return}
                self.send(["id":id ?? 0,"action":"configure","key":action=="choose_rules" ? "rules_path" : action=="choose_log" ? "log_path":"database_path","path":url.path])
            };return
        }
        if action=="export" {
            let panel=NSSavePanel();panel.nameFieldStringValue="Arcana match review.md";panel.allowedContentTypes=[UTType(filenameExtension:"md") ?? .plainText]
            panel.beginSheetModal(for:window) { response in
                guard response == .OK,let url=panel.url else {self.reply(id,result:["cancelled":true]);return}
                do { try (body["text"] as? String ?? "").write(to:url,atomically:true,encoding:.utf8);self.reply(id,result:["saved":true]);NSWorkspace.shared.activateFileViewerSelecting([url]) }
                catch {self.reply(id,error:"The report could not be saved: \(error.localizedDescription)")}
            };return
        }
        if action=="open_data" {
            let url=FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support/Arcana")
            NSWorkspace.shared.open(url);reply(id,result:[:]);return
        }
        if action=="open_link" {
            let links=["arena":"https://magic.wizards.com/en/mtgarena", "logs":"https://mtgarena-support.wizards.com/hc/en-us/articles/360000726823-Creating-Log-Files-on-PC-Mac-Steam", "rules":"https://magic.wizards.com/en/rules"]
            if let key=body["key"] as? String, let value=links[key],let url=URL(string:value){NSWorkspace.shared.open(url)}
            reply(id,result:[:]);return
        }
        if action=="follow_arena" {setFollowing(!followsArena);reply(id,result:["enabled":followsArena]);return}
        if action=="compact" {toggleCompact();reply(id,result:["compact":compactMode]);return}
        if action=="float" {toggleFloat();reply(id,result:["floating":window.level == .floating]);return}
        if action=="launch_arena" {
            if let app=NSRunningApplication.runningApplications(withBundleIdentifier:"com.wizards.mtga").first {
                app.activate(options:[.activateAllWindows,.activateIgnoringOtherApps]);reply(id,result:[:]);return
            }
            let candidates=["/Applications/MTGA.app","/Applications/Magic The Gathering Arena.app",FileManager.default.homeDirectoryForCurrentUser.path+"/Applications/MTGA.app","/Users/Shared/Epic Games/MagicTheGathering/MTGA.app"]
            if let path=candidates.first(where:{FileManager.default.fileExists(atPath:$0)}) {
                NSWorkspace.shared.openApplication(at:URL(fileURLWithPath:path),configuration:NSWorkspace.OpenConfiguration()){_,error in DispatchQueue.main.async{self.reply(id,result:[:],error:error?.localizedDescription)}}
            } else {reply(id,error:"Arena was not found in the usual Applications locations. Open it from your launcher, or use Get Arena in Connection.")};return
        }
        send(body)
    }
    func send(_ body:[String:Any]) {
        guard let data=try? JSONSerialization.data(withJSONObject:body) else{return}
        guard let engine=engine,engine.isRunning else {reply(body["id"],error:"The engine is unavailable. Quit and reopen Arcana.");return}
        io.async { [weak self] in
            do { try self?.input?.fileHandleForWriting.write(contentsOf:data+Data([10])) }
            catch { DispatchQueue.main.async {self?.reply(body["id"],error:"Could not reach the companion engine.")} }
        }
    }
    func webView(_ webView:WKWebView,decidePolicyFor navigationAction:WKNavigationAction,decisionHandler:@escaping (WKNavigationActionPolicy)->Void) {
        guard let url=navigationAction.request.url else{decisionHandler(.cancel);return}
        decisionHandler(url.isFileURL && url.path.hasPrefix(resources.appendingPathComponent("web").path+"/") ? .allow : .cancel)
    }
}
@main
struct ArcanaMain {
    static func main() {
        let app=NSApplication.shared
        let delegate=AppDelegate()
        app.delegate=delegate
        withExtendedLifetime(delegate) { app.run() }
    }
}
