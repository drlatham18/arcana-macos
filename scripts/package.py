#!/usr/bin/env python3
from pathlib import Path
import hashlib, shutil, subprocess, zipfile
ROOT=Path(__file__).resolve().parents[1]
DIST=ROOT/'dist'

def main():
    app=DIST/'Arcana.app'
    if not app.is_dir():raise SystemExit('Run scripts/build.py first.')
    stage=DIST/'installer-content'
    if stage.exists():shutil.rmtree(stage)
    stage.mkdir()
    shutil.copytree(app,stage/'Arcana.app',symlinks=True)
    (stage/'Applications').symlink_to('/Applications')
    (stage/'START HERE.txt').write_text('''ARCANA FOR MACOS

Drag Arcana into Applications, then open it.

LEARNING TOOL
Do not use live coaching in competitive, ranked, tournament, or prize matches.
Background postgame recording remains available for every logged game.
Coaching defaults off; enable it only for permitted noncompetitive practice.
Import your own Wizards rules text in Connection for offline rules search.

COMPACT COMPANION
Click Compact to collapse to a small floating analysis window.
Use the expand button to restore the full app.
Command-Shift-M toggles compact mode. Drag or resize the window beside Arena.
Follow Arena is on by default: the companion travels with the game across
full-screen desktops and displays. The ◎ button turns following off or on.

CONNECT ARENA
In Arena: Settings > View Account > Detailed Logs (Plugin Support).
Restart Arena. Arcana reads the game as you play.
If needed, choose Player.log and the card database in Connection.
Try Explore demo to preview the companion before playing.

SHARING
This package works on Apple Silicon and Intel with macOS 13 or newer.
Share the DMG or ZIP. Your personal history and notes are not inside the app.

FIRST OPEN ON ANOTHER MAC
This build is ad-hoc signed, not Apple-notarized.
If you trust this copy and macOS blocks it, attempt to open it, then use
System Settings > Privacy & Security > Open Anyway for Arcana.
Apple's guide: https://support.apple.com/en-us/102445

CLOSE VS QUIT
Closing the window keeps watching in the menu bar. Command-Q quits.
No Python installation, account, subscription, or API key is required.

Unofficial MTG Arena companion. Not affiliated with Wizards of the Coast.
''',encoding='utf-8')
    subprocess.run(['ditto','-c','-k','--sequesterRsrc','--keepParent',str(app),str(DIST/'Arcana-1.3-universal.zip')],check=True)
    with zipfile.ZipFile(DIST/'Arcana-1.3-source.zip','w',zipfile.ZIP_DEFLATED) as output:
        for path in ROOT.rglob('*'):
            relative=path.relative_to(ROOT)
            if any(x in {'vendor','dist','build','__pycache__','.cache','data','.git'} for x in relative.parts):continue
            if path.is_file() and path.suffix!='.pyc':output.write(path,Path('arcana-macos')/relative)
    subprocess.run(['hdiutil','create','-volname','Arcana','-srcfolder',str(stage),'-ov','-format','UDZO',str(DIST/'Arcana-1.3-universal.dmg')],check=True)
    files=sorted(DIST.glob('Arcana-1.3-*'))
    (DIST/'SHA256SUMS.txt').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+p.name+'\n' for p in files if p.is_file()))
    print('Ready to share:',*[str(p) for p in files],sep='\n')
if __name__=='__main__':main()
