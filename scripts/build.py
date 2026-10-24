import os
import subprocess
import urllib.request
import glob
import base64
import shutil
import json

# 集中讀取全域環境設定；Workflow 的共用設定統一放在最上層 env。
def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"缺少必要環境變數：{name}")
    return value

UPSTREAM_REPO = required_env("UPSTREAM_REPO")
PACKAGE_NAME = required_env("PACKAGE_NAME")
UPSTREAM_VERSION = required_env("UPSTREAM_VERSION")
RELEASE_BODY_BASE64 = os.environ.get("RELEASE_BODY_BASE64", "")
release_body = base64.b64decode(RELEASE_BODY_BASE64.encode('utf-8')).decode('utf-8') if RELEASE_BODY_BASE64 else ""
DEBFULLNAME = required_env("DEBFULLNAME")
DEBEMAIL = required_env("DEBEMAIL")
BUILD_DEPENDS = required_env("BUILD_DEPENDS")
BINARY_DEPENDS = os.environ.get("BINARY_DEPENDS", "")
GPG_FINGERPRINT = required_env("GPG_FINGERPRINT")

# 判斷觸發類型與選單勾選狀態
EVENT_NAME = os.environ.get("EVENT_NAME", "")
if EVENT_NAME == "workflow_dispatch":
    CHOSEN_2204 = os.environ.get("CHOSEN_2204", "true").lower() == "true"
    CHOSEN_2404 = os.environ.get("CHOSEN_2404", "true").lower() == "true"
    CHOSEN_2604 = os.environ.get("CHOSEN_2604", "true").lower() == "true"
else:
    CHOSEN_2204 = os.environ.get("AUTO_BUILD_2204", "true").lower() == "true"
    CHOSEN_2404 = os.environ.get("AUTO_BUILD_2404", "true").lower() == "true"
    CHOSEN_2604 = os.environ.get("AUTO_BUILD_2604", "true").lower() == "true"

# 完整的發行版候選列表
ALL_DISTROS = [
    {
        "ubuntu_version": "22.04",
        "codename": "jammy",
        "revision": os.environ.get("REV_2204", ""),
        "dpkg_flag": os.environ.get("DPKG_FLAG_2204", "").strip() or "-sa",
        "enabled": CHOSEN_2204
    },
    {
        "ubuntu_version": "24.04",
        "codename": "noble",
        "revision": os.environ.get("REV_2404", ""),
        "dpkg_flag": os.environ.get("DPKG_FLAG_2404", "").strip() or "-sa",
        "enabled": CHOSEN_2404
    },
    {
        "ubuntu_version": "26.04",
        "codename": "resolute",
        "revision": os.environ.get("REV_2604", ""),
        "dpkg_flag": os.environ.get("DPKG_FLAG_2604", "").strip() or "-sa",
        "enabled": CHOSEN_2604
    }
]

# 篩選出本次需要執行打包的版本
DISTROS = [d.copy() for d in ALL_DISTROS if d["enabled"]]

# 相同 upstream tarball 只需要由一個系列攜帶 -sa；其餘系列使用 -sd，避免重複上傳同一個 .orig.tar.gz。
if DISTROS and not any(d["dpkg_flag"].strip().strip("\"").strip("'") == "-sa" for d in DISTROS):
    print(f"沒有任何系列設定 -sa，將由 {DISTROS[0]['codename']} 首次攜帶 orig.tar.gz。")
    DISTROS[0]["dpkg_flag"] = "-sa"
seen_source_tarball = False
for distro in DISTROS:
    flag = distro["dpkg_flag"].strip().strip("\"").strip("'")
    if flag == "-sa":
        if not seen_source_tarball:
            seen_source_tarball = True
        else:
            print(f"[{distro['codename']}] 已有其他系列使用 -sa，為避免重複上傳 orig.tar.gz，改用 -sd。")
            distro["dpkg_flag"] = "-sd"

if not DISTROS:
    raise SystemExit("沒有選擇任何需要打包的 Ubuntu 版本；請至少勾選一個系列。")

# 初始化或清空統一的打包輸出目錄
build_dir = "build_dir"
if os.path.exists(build_dir):
    shutil.rmtree(build_dir)
os.makedirs(build_dir)

# 獲取當前工作目錄的絕對路徑
root_dir = os.getcwd()
absolute_build_dir = os.path.abspath(build_dir)

# 將本次實際執行的版本與 flag 資訊記錄成 JSON，供後續 Workflow 步驟排序使用
flags_summary = []
for d in DISTROS:
    flags_summary.append({
        "codename": d["codename"],
        "ubuntu_version": d["ubuntu_version"],
        "dpkg_flag": d["dpkg_flag"].strip().strip('"\'')
    })
with open(os.path.join(absolute_build_dir, "distro_flags.json"), "w", encoding="utf-8") as jf:
    json.dump(flags_summary, jf)

# ==================== 步驟 1: 外部一次性下載原始碼 ====================
print("=== 正在進行一次性原始碼 Tarball 下載 ===")
tag_name = f"v{UPSTREAM_VERSION}"
tarball_url = f"https://github.com/{UPSTREAM_REPO}/archive/refs/tags/{tag_name}.tar.gz"
orig_tarball_name = f"{PACKAGE_NAME}_{UPSTREAM_VERSION}.orig.tar.gz"
shared_tarball_path = os.path.join(absolute_build_dir, orig_tarball_name)

try:
    print(f"正在下載主網址: {tarball_url}")
    urllib.request.urlretrieve(tarball_url, shared_tarball_path)
except Exception:
    tarball_url = f"https://github.com/{UPSTREAM_REPO}/archive/refs/tags/{UPSTREAM_VERSION}.tar.gz"
    print(f"嘗試備用下載網址: {tarball_url}")
    urllib.request.urlretrieve(tarball_url, shared_tarball_path)

print(f"原始碼成功儲存至: {shared_tarball_path}\n")

# ==================== 步驟 2: 迴圈處理各發行版打包 ====================
for distro in DISTROS:
    ubuntu_version = distro["ubuntu_version"]
    codename = distro["codename"]
    revision = distro["revision"] if distro["revision"] != "" else "0"
    dpkg_flag = distro["dpkg_flag"].strip().strip('"\'')
    
    debian_version = f"{UPSTREAM_VERSION}-{revision}~ubuntu{ubuntu_version}"
    print(f"=== [單機串行] 正在處理 Ubuntu {ubuntu_version} ({codename}), 版號: {debian_version} ===")
    
    # 針對各發行版建立獨立的工作子目錄
    distro_work_dir = os.path.join(absolute_build_dir, f"work_{codename}")
    os.makedirs(distro_work_dir, exist_ok=True)
    os.chdir(distro_work_dir)
    
    # 複製共享的 orig.tar.gz 到各自的工作目錄中
    local_tarball_path = os.path.join(distro_work_dir, orig_tarball_name)
    shutil.copy2(shared_tarball_path, local_tarball_path)
    
    # 解壓縮套件
    subprocess.run(["tar", "-xzf", local_tarball_path], check=True)
    
    extracted_dir = None
    for d in os.listdir('.'):
        if os.path.isdir(d) and d != orig_tarball_name:
            extracted_dir = d
            break
            
    if not extracted_dir:
        raise RuntimeError(f"[{codename}] 找不到解壓後的原始碼目錄！")
        
    os.chdir(extracted_dir)
    
    # 建立 debian/ 設定結構
    os.makedirs("debian/source", exist_ok=True)
    with open("debian/source/format", "w", encoding="utf-8") as f:
        f.write("3.0 (quilt)\n")
        
    control_content = f"""Source: {PACKAGE_NAME}
Section: utils
Priority: optional
Maintainer: {DEBFULLNAME} <{DEBEMAIL}>
Build-Depends: {BUILD_DEPENDS}
Standards-Version: 4.6.2

Package: {PACKAGE_NAME}
Architecture: any
Depends: {BINARY_DEPENDS}
Description: McBopomofo (小麥注音) engine for Fcitx5
 Fcitx 5 version of the McBopomofo traditional Chinese input method.
 Provides an intelligent Zhuyin/Bopomofo input experience.
"""
    with open("debian/control", "w", encoding="utf-8") as f:
        f.write(control_content)
        
    rules_content = r"""#!/usr/bin/make -f
%:
	dh $@ --buildsystem=cmake

override_dh_auto_configure:
	dh_auto_configure -- -G Ninja -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release -DENABLE_TEST=OFF
"""
    with open("debian/rules", "w", encoding="utf-8") as f:
        f.write(rules_content)
    os.chmod("debian/rules", 0o755)
    
    copyright_content = f"""Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: {PACKAGE_NAME}
Upstream-Contact: OpenVanilla Project
Source: https://github.com/{UPSTREAM_REPO}

Files: *
Copyright: Copyright (c) 2022 and onwards The McBopomofo Authors
License: MIT
 Permission is hereby granted, free of charge, to any person obtaining a copy
 of this software and associated documentation files (the "Software"), to deal
 in the Software without restriction, including without limitation the rights
 to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 copies of the Software, and to permit persons to whom the Software is
 furnished to do so, subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in all
 copies or substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 SOFTWARE.
"""
    with open("debian/copyright", "w", encoding="utf-8") as f:
        f.write(copyright_content)
        
    date_str = subprocess.check_output(['date', '-R']).decode().strip()
    changelog_body = ""
    for line in release_body.splitlines():
        changelog_body += f"  * {line}\n"
    if not changelog_body.strip():
        changelog_body = "  * Automated release build from upstream.\n"
        
    changelog_content = f"""{PACKAGE_NAME} ({debian_version}) {codename}; urgency=medium

{changelog_body}
 -- {DEBFULLNAME} <{DEBEMAIL}>  {date_str}
"""
    with open("debian/changelog", "w", encoding="utf-8") as f:
        f.write(changelog_content)
        
    print(f"[{codename}] 已建立所有 debian/ 打包設定檔，開始編譯 Source 套件...")
    subprocess.run(["dpkg-buildpackage", "-S", dpkg_flag, "-us", "-uc"], check=True)
    
    # 回到工作子目錄層找 changes
    os.chdir("../")
    changes_files = glob.glob(f"{PACKAGE_NAME}_{debian_version}_source.changes")
    if not changes_files:
        raise RuntimeError(f"[{codename}] 找不到打包完成的 .changes 檔案！")
    changes_file = changes_files[0]
    
    # 簽署套件：支援有密碼與無密碼的 GPG 私鑰。
    os.environ["GNUPGHOME"] = os.path.expanduser("~/.gnupg")
    signer_path = os.path.join(root_dir, "build_dir", "gpg-sign-wrapper.sh")
    if not os.path.exists(signer_path):
        with open(signer_path, "w", encoding="utf-8") as signer:
            signer.write("#!/usr/bin/env bash\nset -euo pipefail\nif [[ -n \"${GPG_PASSPHRASE:-}\" ]]; then\n  exec gpg --batch --pinentry-mode loopback --passphrase-fd 3 \"$@\" 3<<<\"$GPG_PASSPHRASE\"\nelse\n  exec gpg --batch \"$@\"\nfi\n")
        os.chmod(signer_path, 0o700)
    print(f"[{codename}] 使用 GPG 金鑰簽署中...")
    subprocess.run(["debsign", "-p", signer_path, "-k", GPG_FINGERPRINT, changes_file], check=True)
    
    # 將所有打包產出移出到 build_dir 根目錄下
    for generated_file in glob.glob(f"{PACKAGE_NAME}_{UPSTREAM_VERSION}*") + glob.glob(f"{PACKAGE_NAME}_{debian_version}*"):
        if os.path.isfile(generated_file):
            dest_path = os.path.join(absolute_build_dir, os.path.basename(generated_file))
            # 如果目的地已經有同名檔案（例如共用的 .orig.tar.gz），則直接略過不重複搬移
            if os.path.exists(dest_path):
                print(f"[{codename}] 目的地已存在 {os.path.basename(generated_file)}，跳過搬移。")
                continue
            shutil.move(generated_file, absolute_build_dir)
            
    print(f"[{codename}] 套件打包與簽署成功，產出已匯總。\n")

print("=== 所有選擇之發行版打包簽署完畢。 ===")
