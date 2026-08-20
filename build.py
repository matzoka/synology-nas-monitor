import os
import py_compile
import shutil
import tarfile
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
OUT = os.path.join(ROOT, "dist")
os.makedirs(OUT, exist_ok=True)

for mod in ("server.py", "store.py", "syno.py", "telegram.py", "alerts.py"):
    cfile = os.path.join(OUT, f"_check_{mod}.pyc")
    py_compile.compile(os.path.join(SRC, "package", "nasmon", mod), cfile=cfile, doraise=True)
    os.remove(cfile)
print("python syntax OK")

pycache = os.path.join(SRC, "package", "nasmon", "__pycache__")
if os.path.isdir(pycache):
    shutil.rmtree(pycache)


def make_icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size // 5
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=(26, 26, 30, 255))
    m = size // 4
    bw = size // 8
    gap = size // 16
    base = size - m
    heights = [size // 5, size // 3, size // 2]
    for i, h in enumerate(heights):
        x0 = m + i * (bw + gap)
        d.rounded_rectangle([x0, base - h, x0 + bw, base], radius=bw // 3, fill=(240, 240, 245, 255))
    return img


make_icon(72).save(os.path.join(OUT, "PACKAGE_ICON.PNG"))
make_icon(256).save(os.path.join(OUT, "PACKAGE_ICON_256.PNG"))
print("icons OK")

STAGE = os.path.join(OUT, "stage")
if os.path.isdir(STAGE):
    shutil.rmtree(STAGE)
shutil.copytree(os.path.join(SRC, "ui"), os.path.join(STAGE, "ui"))
os.makedirs(os.path.join(STAGE, "ui", "images"), exist_ok=True)
for size in (16, 24, 32, 48, 64, 72, 256):
    make_icon(size).save(os.path.join(STAGE, "ui", "images", f"icon_{size}.png"))
print("ui staged OK")

def normalize(executable_dirs=("scripts",)):
    def filt(info):
        if "__pycache__" in info.name or info.name.endswith(".pyc"):
            return None
        info.uid = 0
        info.gid = 0
        info.uname = "root"
        info.gname = "root"
        top = info.name.split("/")[0]
        if info.isdir():
            info.mode = 0o755
        elif top in executable_dirs:
            info.mode = 0o755
        else:
            info.mode = 0o644
        return info
    return filt


pkg_tgz = os.path.join(OUT, "package.tgz")
with tarfile.open(pkg_tgz, "w:gz", format=tarfile.GNU_FORMAT) as tar:
    tar.add(os.path.join(SRC, "package", "nasmon"), arcname="nasmon", filter=normalize())
    tar.add(os.path.join(STAGE, "ui"), arcname="ui", filter=normalize())
print("package.tgz OK")

info = {}
with open(os.path.join(SRC, "INFO"), encoding="utf-8") as f:
    for line in f:
        if "=" in line:
            key, value = line.rstrip("\n").split("=", 1)
            info[key] = value.strip().strip('"')

spk = os.path.join(OUT, f'{info["package"]}-{info["version"]}.spk')
with tarfile.open(spk, "w", format=tarfile.GNU_FORMAT) as tar:
    tar.add(os.path.join(SRC, "INFO"), arcname="INFO", filter=normalize())
    tar.add(os.path.join(OUT, "PACKAGE_ICON.PNG"), arcname="PACKAGE_ICON.PNG", filter=normalize())
    tar.add(os.path.join(OUT, "PACKAGE_ICON_256.PNG"), arcname="PACKAGE_ICON_256.PNG", filter=normalize())
    tar.add(os.path.join(SRC, "conf"), arcname="conf", filter=normalize())
    tar.add(os.path.join(SRC, "scripts"), arcname="scripts", filter=normalize())
    tar.add(pkg_tgz, arcname="package.tgz", filter=normalize())
print("spk OK:", spk)

with open(spk, "rb") as f:
    head = f.read(300)
assert head[:2] != b"\x1f\x8b", "outer tar must NOT be gzipped"
assert head[257:262] == b"ustar", "outer tar must be ustar format"
print("outer tar: uncompressed ustar OK")

with tarfile.open(spk) as tar:
    for m in tar.getmembers():
        print(" ", m.name, m.size, oct(m.mode))
