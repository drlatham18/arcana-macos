#!/usr/bin/env python3
"""Build a universal macOS app. Requires Apple's Command Line Tools only."""
from pathlib import Path
import hashlib, os, plistlib, shutil, struct, subprocess, tarfile, urllib.request
ROOT=Path(__file__).resolve().parents[1]
APP=ROOT/'dist/Arcana.app'
RES=APP/'Contents/Resources'
RUNTIMES={
 'arm64':('aarch64','81a359f1cfadd4da11766534c5913791cea55f26e1bb902cacd2a531bb1e4b2b'),
 'x86_64':('x86_64','65b195c9cedc1fef6767f044f9822069adbd1bd9204d424ece4628776fdc04bb')}

def run(*args): subprocess.run(args,check=True)

def main():
 ROOT.joinpath('build').mkdir(exist_ok=True)
 for arch,(upstream,digest) in RUNTIMES.items():
  target=ROOT/'vendor'/arch
  if not (target/'python/bin/python3').exists():
   target.mkdir(parents=True,exist_ok=True)
   archive=ROOT/'build'/f'python-{arch}.tar.gz'
   url=f'https://github.com/astral-sh/python-build-standalone/releases/download/20260901/cpython-3.12.14%2B20260901-{upstream}-apple-darwin-install_only_stripped.tar.gz'
   urllib.request.urlretrieve(url,archive)
   if hashlib.sha256(archive.read_bytes()).hexdigest()!=digest: raise RuntimeError('Runtime checksum mismatch')
   with tarfile.open(archive) as bundle:
    # Modern Python validates links and traversal. Older system Python extracts
    # only the immutable upstream archive verified by the pinned SHA-256 above.
    if hasattr(tarfile, 'data_filter'): bundle.extractall(target,filter='data')
    else: bundle.extractall(target)
 if APP.exists(): shutil.rmtree(APP)
 (APP/'Contents/MacOS').mkdir(parents=True)
 shutil.copytree(ROOT/'Resources',RES,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.cache','data'))
 for arch in RUNTIMES:
  shutil.copytree(ROOT/'vendor'/arch/'python',RES/'runtime'/arch/'python',symlinks=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
  run('xcrun','swiftc','-O','-swift-version','5','-target',f'{arch}-apple-macos13.0','-module-cache-path',str(ROOT/'build/module-cache'),str(ROOT/'Sources/Arcana.swift'),str(ROOT/'Sources/ArenaPlacement.swift'),'-framework','Cocoa','-framework','WebKit','-o',str(ROOT/'build'/f'Arcana-{arch}'))
 run('lipo','-create',str(ROOT/'build/Arcana-arm64'),str(ROOT/'build/Arcana-x86_64'),'-output',str(APP/'Contents/MacOS/Arcana'))
 info={'CFBundleName':'Arcana','CFBundleDisplayName':'Arcana','CFBundleIdentifier':'com.drlatham.arcana','CFBundleExecutable':'Arcana','CFBundlePackageType':'APPL','CFBundleShortVersionString':'1.2','CFBundleVersion':'3','LSMinimumSystemVersion':'13.0','NSHighResolutionCapable':True,'CFBundleIconFile':'Arcana','NSHumanReadableCopyright':'Arcana · Unofficial MTG Arena companion. Magic: The Gathering is a trademark of Wizards of the Coast.'}
 with (APP/'Contents/Info.plist').open('wb') as handle:plistlib.dump(info,handle)
 iconset=ROOT/'build/Arcana.iconset';iconset.mkdir(exist_ok=True)
 run('xcrun','swiftc','-module-cache-path',str(ROOT/'build/module-cache'),str(ROOT/'scripts/icon.swift'),'-o',str(ROOT/'build/make-icon'))
 run(str(ROOT/'build/make-icon'),str(iconset))
 entries=[]
 for kind,name in [('icp4','16x16'),('icp5','32x32'),('icp6','32x32@2x'),('ic07','128x128'),('ic08','256x256'),('ic09','512x512'),('ic10','512x512@2x')]:
  png=(iconset/('icon_'+name+'.png')).read_bytes()
  entries.append(kind.encode()+struct.pack('>I',len(png)+8)+png)
 payload=b''.join(entries)
 (RES/'Arcana.icns').write_bytes(b'icns'+struct.pack('>I',len(payload)+8)+payload)
 for path in sorted(RES.rglob('*')):
  if path.is_file() and not path.is_symlink():
   with path.open('rb') as handle: magic=handle.read(4)
   if magic in (b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe'):
    run('codesign','--force','--sign','-',str(path))
 run('codesign','--force','--sign','-',str(APP))
 run('codesign','--verify','--deep','--strict',str(APP))
 print('Built',APP)
if __name__=='__main__':main()
