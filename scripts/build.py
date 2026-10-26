#!/usr/bin/env python3
"""為 Launchpad PPA 產生並簽署 Ubuntu Source 套件（.dsc / .changes）。

所有設定都從環境變數讀取（由 ppa-build.yaml 注入）。

必要：UPSTREAM_REPO PACKAGE_NAME UPSTREAM_VERSION DEBFULLNAME DEBEMAIL
      BUILD_DEPENDS GPG_FINGERPRINT（SKIP_SIGN=true 時可省略）
選用：UPSTREAM_TAG RELEASE_BODY_BASE64 BINARY_DEPENDS GPG_PASSPHRASE
      REV_<2204|2404|2604> DPKG_FLAG_<2204|2404|2604>
      EVENT_NAME CHOSEN_<...>（手動觸發）/ AUTO_BUILD_<...>（排程觸發）
選用：PPA_TARGET + ENABLE_UPLOAD=true 時，會優先沿用 PPA 已有的 orig.tar.gz（SKIP_PPA_ORIG=true 可停用）
測試用：UPSTREAM_TARBALL=<本機路徑>（略過下載）、SKIP_SIGN=true（略過簽署）

產出：build_dir/ 內的 source 套件檔案，以及 build_dir/distro_flags.json
      （依上傳順序排列，-sa 一定在最前面，供 Workflow 上傳步驟使用）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path

STANDARDS_VERSION = "4.6.2"
VALID_FLAGS = {"-sa", "-sd", "-si"}
MAX_CHANGELOG_LINES = 200

# (環境變數後綴, Ubuntu 版本, 代號)
SERIES = (
    ("2204", "22.04", "jammy"),
    ("2404", "24.04", "noble"),
    ("2604", "26.04", "resolute"),
)

SIGN_WRAPPER = """#!/usr/bin/env bash
set -euo pipefail
if [[ -n "${GPG_PASSPHRASE:-}" ]]; then
  exec gpg --batch --pinentry-mode loopback --passphrase-fd 3 "$@" 3<<<"$GPG_PASSPHRASE"
else
  exec gpg --batch "$@"
fi
"""

COPYRIGHT_TEMPLATE = """Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: {package}
Upstream-Contact: OpenVanilla Project
Source: https://github.com/{repo}

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

Files: debian/*
Copyright: Copyright (c) {year} {maintainer}
License: MIT
"""


# ----------------------------------------------------------------------------
# 基本工具
# ----------------------------------------------------------------------------
def log(message: str = "") -> None:
    print(message, flush=True)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(f"缺少必要環境變數：{name}")
    return value


def env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def split_csv(value: str) -> list[str]:
    """把以逗號分隔的依賴字串拆成乾淨的清單（同時消除換行與多餘空白）。"""
    return [" ".join(part.split()) for part in value.split(",") if part.strip()]


def run(cmd: list[str], cwd: Path | None = None) -> None:
    log(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd, check=True)


def write_text(path: Path, content: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def link_or_copy(src: Path, dst: Path) -> None:
    """優先用 hard link（不佔額外空間），失敗再退回複製。"""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


# ----------------------------------------------------------------------------
# 發行版設定
# ----------------------------------------------------------------------------
@dataclass
class Distro:
    ubuntu_version: str
    codename: str
    revision: str
    flag: str
    debian_version: str
    changes: str = ""


def normalize_flag(raw: str) -> str:
    flag = raw.strip().strip("\"'").strip() or "-sa"
    if flag not in VALID_FLAGS:
        raise SystemExit(f"不支援的 dpkg-genchanges 旗標：{flag}（可用：{', '.join(sorted(VALID_FLAGS))}）")
    return flag


def load_distros(upstream_version: str) -> list[Distro]:
    # 手動觸發看選單；排程觸發看 check-version job 的判斷結果。
    prefix = "CHOSEN" if os.environ.get("EVENT_NAME", "") == "workflow_dispatch" else "AUTO_BUILD"
    distros: list[Distro] = []
    for suffix, ubuntu_version, codename in SERIES:
        if not env_bool(f"{prefix}_{suffix}", True):
            continue
        revision = os.environ.get(f"REV_{suffix}", "").strip() or "0"
        distros.append(
            Distro(
                ubuntu_version=ubuntu_version,
                codename=codename,
                revision=revision,
                flag=normalize_flag(os.environ.get(f"DPKG_FLAG_{suffix}", "")),
                debian_version=f"{upstream_version}-{revision}~ubuntu{ubuntu_version}",
            )
        )
    if not distros:
        raise SystemExit("沒有選擇任何需要打包的 Ubuntu 版本；請至少勾選一個系列。")
    return distros


def assign_orig_flags(distros: list[Distro]) -> None:
    """同一份 orig.tar.gz 只讓一個系列攜帶 -sa，其餘改 -sd；並把 -sa 排到最前面。"""
    if not any(d.flag == "-sa" for d in distros):
        log(f"沒有任何系列設定 -sa，將由 {distros[0].codename} 首次攜帶 orig.tar.gz。")
        distros[0].flag = "-sa"
    seen = False
    for distro in distros:
        if distro.flag != "-sa":
            continue
        if seen:
            log(f"[{distro.codename}] 已有其他系列使用 -sa，為避免重複上傳 orig.tar.gz，改用 -sd。")
            distro.flag = "-sd"
        seen = True
    distros.sort(key=lambda d: d.flag != "-sa")  # stable sort：-sa 優先，其餘維持原順序


# ----------------------------------------------------------------------------
# 原始碼下載
# ----------------------------------------------------------------------------
def fetch(url: str, dest: Path, retries: int = 3) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "ppa-build-script"})
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, dest.open("wb") as out:
                shutil.copyfileobj(response, out)
            return
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # 不存在就不用重試，交給呼叫端試下一個網址
                raise
            last_error = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt < retries:
            wait = 2**attempt
            log(f"下載失敗（{last_error}），{wait} 秒後重試（{attempt}/{retries}）...")
            time.sleep(wait)
    assert last_error is not None
    raise last_error


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def top_level_dir(tarball: Path) -> str:
    """回傳 tarball 唯一的頂層目錄名稱；不是單一頂層目錄就中止。"""
    names: set[str] = set()
    with tarfile.open(tarball, "r:*") as archive:
        for member in archive:
            parts = Path(member.name).parts
            if parts and parts[0] not in {"/", ".."}:
                names.add(parts[0])
    if len(names) != 1:
        raise RuntimeError(f"原始碼 tarball 應只有一個頂層目錄，實際為：{sorted(names)}")
    return names.pop()


def lp_json(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "ppa-build-script", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def try_reuse_ppa_orig(package: str, version: str, orig_name: str, dest: Path) -> bool:
    """PPA 已有同一個上游版本的 orig.tar.gz 時直接沿用。

    GitHub 的 archive tarball 不保證每次位元組相同；Launchpad 會拒絕「同名但內容不同」的 orig，
    因此只要 PPA 已經有這份檔案，就必須用它，而不是重新下載。任何失敗都安靜退回 GitHub 下載。
    """
    target = os.environ.get("PPA_TARGET", "").strip()
    if not target or not env_bool("ENABLE_UPLOAD", False) or env_bool("SKIP_PPA_ORIG", False):
        return False
    try:
        owner, _, name = target.removeprefix("ppa:").partition("/")
        archive = f"https://api.launchpad.net/1.0/~{owner.lstrip('~')}/+archive/ubuntu/{name or 'ppa'}"
        query = urllib.parse.urlencode({
            "ws.op": "getPublishedSources", "source_name": package, "exact_match": "true", "ws.size": "100"})
        url: str | None = f"{archive}?{query}"
        while url:
            page = lp_json(url)
            for entry in page.get("entries", []):
                if not entry.get("source_package_version", "").startswith(f"{version}-"):
                    continue
                files_url = f"{entry['self_link']}?{urllib.parse.urlencode({'ws.op': 'sourceFileUrls'})}"
                for file_url in lp_json(files_url):
                    if file_url.rsplit("/", 1)[-1] != orig_name:
                        continue
                    log(f"PPA 已有 {orig_name}，沿用以確保內容一致：{file_url}")
                    fetch(file_url, dest)
                    if tarfile.is_tarfile(dest):
                        return True
                    dest.unlink(missing_ok=True)
            url = page.get("next_collection_link")
    except Exception as exc:  # noqa: BLE001 - 查不到就退回 GitHub 下載
        log(f"查詢 PPA 既有 orig 失敗（{exc}），改從 GitHub 下載。")
        dest.unlink(missing_ok=True)
    return False


def download_upstream(repo: str, version: str, tag: str, dest: Path) -> None:
    local = os.environ.get("UPSTREAM_TARBALL", "").strip()
    if local:
        log(f"使用本機 tarball：{local}")
        shutil.copy2(local, dest)
        return

    candidates = [tag] if tag else [f"v{version}", version]
    last_error: Exception | None = None
    for candidate in candidates:
        url = f"https://github.com/{repo}/archive/refs/tags/{candidate}.tar.gz"
        log(f"正在下載：{url}")
        try:
            fetch(url, dest)
        except Exception as exc:  # noqa: BLE001 - 任何失敗都改試下一個候選網址
            log(f"  失敗：{exc}")
            last_error = exc
            continue
        if tarfile.is_tarfile(dest):
            return
        log("  下載內容不是有效的 tar 檔，改試下一個候選網址。")
    raise RuntimeError(f"無法下載原始碼（已嘗試：{', '.join(candidates)}）") from last_error


# ----------------------------------------------------------------------------
# debian/ 檔案內容
# ----------------------------------------------------------------------------
def render_control(package: str, repo: str, maintainer: str, email: str,
                   build_depends: list[str], binary_depends: list[str]) -> str:
    # ${shlibs:Depends} / ${misc:Depends} 由 dh 自動展開；缺少時補上，避免出現空的 Depends 欄位。
    depends = list(binary_depends)
    for required in ("${shlibs:Depends}", "${misc:Depends}"):
        if required not in depends:
            depends.append(required)
    return f"""Source: {package}
Section: utils
Priority: optional
Maintainer: {maintainer} <{email}>
Build-Depends: {", ".join(build_depends)}
Standards-Version: {STANDARDS_VERSION}
Rules-Requires-Root: no
Homepage: https://github.com/{repo}

Package: {package}
Architecture: any
Depends: {", ".join(depends)}
Description: McBopomofo (小麥注音) engine for Fcitx5
 Fcitx 5 version of the McBopomofo traditional Chinese input method.
 Provides an intelligent Zhuyin/Bopomofo input experience.
"""


RULES = r"""#!/usr/bin/make -f
%:
	dh $@ --buildsystem=cmake+ninja

override_dh_auto_configure:
	dh_auto_configure -- -DCMAKE_INSTALL_PREFIX=/usr -DCMAKE_BUILD_TYPE=Release -DENABLE_TEST=OFF
"""


def format_changelog_lines(release_body: str) -> str:
    """把 GitHub Release 內文整理成合法、整齊的 changelog 條目。"""
    entries: list[str] = []
    for raw in release_body.replace("\r", "").splitlines():
        line = re.sub(r"^\s*(#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)", "", raw).strip()
        line = "".join(ch for ch in line if ch.isprintable())
        if not line:
            continue
        entries.extend(
            textwrap.wrap(
                line,
                width=78,
                initial_indent="  * ",
                subsequent_indent="    ",
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
        if len(entries) >= MAX_CHANGELOG_LINES:
            entries = entries[:MAX_CHANGELOG_LINES]
            entries.append("  * （內容過長，已截斷；完整說明請見上游 Release。）")
            break
    if not entries:
        entries = ["  * Automated release build from upstream."]
    return "\n".join(entries) + "\n"


def render_changelog(package: str, distro: Distro, body: str,
                     maintainer: str, email: str, date_str: str) -> str:
    return (
        f"{package} ({distro.debian_version}) {distro.codename}; urgency=medium\n\n"
        f"{body}\n"
        f" -- {maintainer} <{email}>  {date_str}\n"
    )


def check_build_depends(build_depends: list[str]) -> None:
    names = {re.split(r"[\s(|]", dep, maxsplit=1)[0] for dep in build_depends}
    if "debhelper-compat" not in names:
        raise SystemExit("BUILD_DEPENDS 必須包含 debhelper-compat (= 13) 之類的項目，否則 dh 無法決定 compat level。")
    for needed in ("cmake", "ninja-build"):
        if needed not in names:
            log(f"⚠️ BUILD_DEPENDS 未包含 {needed}，但 debian/rules 需要它，Launchpad 編譯可能失敗。")


# ----------------------------------------------------------------------------
# 單一發行版流程
# ----------------------------------------------------------------------------
def build_source_package(distro: Distro, ctx: dict) -> None:
    package: str = ctx["package"]
    build_dir: Path = ctx["build_dir"]
    orig_name: str = ctx["orig_name"]
    codename = distro.codename

    log(f"=== 正在處理 Ubuntu {distro.ubuntu_version} ({codename})，版號：{distro.debian_version}，旗標：{distro.flag} ===")

    work = build_dir / f"work_{codename}"
    work.mkdir(parents=True)
    link_or_copy(ctx["shared_tarball"], work / orig_name)

    run(["tar", "-xzf", orig_name], cwd=work)
    extracted = work / ctx["top_dir"]
    src = work / f"{package}-{ctx['upstream_version']}"  # dpkg-source 預期的 <source>-<upstream> 目錄名
    if extracted != src:
        extracted.rename(src)

    debian = src / "debian"
    write_text(debian / "source" / "format", "3.0 (quilt)\n")
    write_text(debian / "control", ctx["control"])
    write_text(debian / "rules", RULES, mode=0o755)
    write_text(debian / "copyright", ctx["copyright"])
    write_text(debian / "changelog", render_changelog(
        package, distro, ctx["changelog_body"], ctx["maintainer"], ctx["email"], ctx["date_str"]))

    # Source-only 套件不需要編譯，因此：
    #   -d  不檢查 Build-Depends（Runner 不必安裝 Qt/Fcitx5 等開發套件）
    #   -nc 不執行 debian/rules clean（剛解壓的目錄本來就是乾淨的）
    run(["dpkg-buildpackage", "-S", distro.flag, "-us", "-uc", "-d", "-nc"], cwd=src)

    changes = work / f"{package}_{distro.debian_version}_source.changes"
    if not changes.is_file():
        raise RuntimeError(f"[{codename}] 找不到打包完成的 {changes.name}！")

    if ctx["signer"]:
        log(f"[{codename}] 使用 GPG 金鑰簽署中...")
        run(["debsign", "-p", str(ctx["signer"]), "-k", ctx["fingerprint"], changes.name], cwd=work)
    else:
        log(f"[{codename}] SKIP_SIGN=true，略過簽署。")

    # 搬出本系列專屬產物；共用的 orig.tar.gz 早已在 build_dir，不重複搬移。
    prefix = f"{package}_{distro.debian_version}"
    for produced in sorted(work.iterdir()):
        if produced.is_file() and produced.name.startswith(prefix):
            shutil.move(str(produced), build_dir / produced.name)
    shutil.rmtree(work)

    distro.changes = changes.name
    log(f"[{codename}] 套件打包與簽署成功。\n")


# ----------------------------------------------------------------------------
# 主程式
# ----------------------------------------------------------------------------
def main() -> None:
    repo = required_env("UPSTREAM_REPO")
    package = required_env("PACKAGE_NAME")
    upstream_version = required_env("UPSTREAM_VERSION")
    maintainer = required_env("DEBFULLNAME")
    email = required_env("DEBEMAIL")
    build_depends = split_csv(required_env("BUILD_DEPENDS"))
    binary_depends = split_csv(os.environ.get("BINARY_DEPENDS", ""))
    tag = os.environ.get("UPSTREAM_TAG", "").strip()

    skip_sign = env_bool("SKIP_SIGN", False)
    fingerprint = "" if skip_sign else required_env("GPG_FINGERPRINT")

    encoded_body = os.environ.get("RELEASE_BODY_BASE64", "")
    release_body = base64.b64decode(encoded_body).decode("utf-8") if encoded_body else ""

    check_build_depends(build_depends)
    distros = load_distros(upstream_version)
    assign_orig_flags(distros)

    build_dir = Path("build_dir").resolve()
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir()

    # 只下載一次原始碼，所有發行版共用同一份 orig.tar.gz。
    log("=== 下載原始碼 Tarball ===")
    orig_name = f"{package}_{upstream_version}.orig.tar.gz"
    shared_tarball = build_dir / orig_name
    if try_reuse_ppa_orig(package, upstream_version, orig_name, shared_tarball):
        # orig 已在 PPA，所有系列都不必（也不該）再攜帶它。
        for distro in distros:
            distro.flag = "-sd"
    else:
        download_upstream(repo, upstream_version, tag, shared_tarball)
    log(f"已儲存：{shared_tarball}（sha256: {sha256_of(shared_tarball)}）\n")

    ctx = {
        "package": package,
        "build_dir": build_dir,
        "orig_name": orig_name,
        "shared_tarball": shared_tarball,
        "top_dir": top_level_dir(shared_tarball),
        "upstream_version": upstream_version,
        "maintainer": maintainer,
        "email": email,
        "control": render_control(package, repo, maintainer, email, build_depends, binary_depends),
        "copyright": COPYRIGHT_TEMPLATE.format(
            package=package, repo=repo, year=datetime.now(timezone.utc).year, maintainer=maintainer),
        "changelog_body": format_changelog_lines(release_body),
        "date_str": format_datetime(datetime.now(timezone.utc)),
        "signer": None,
        "fingerprint": fingerprint,
    }

    # 逐一處理各發行版。
    signer_dir = Path(tempfile.mkdtemp(prefix="ppa-sign-"))
    try:
        if not skip_sign:
            os.environ.setdefault("GNUPGHOME", os.path.expanduser("~/.gnupg"))
            signer = signer_dir / "gpg-sign-wrapper.sh"
            write_text(signer, SIGN_WRAPPER, mode=0o700)
            ctx["signer"] = signer
        for distro in distros:
            build_source_package(distro, ctx)
    finally:
        shutil.rmtree(signer_dir, ignore_errors=True)

    # 記錄實際打包的版本，依上傳順序排列，供上傳步驟直接使用。
    summary = [
        {
            "codename": d.codename,
            "ubuntu_version": d.ubuntu_version,
            "dpkg_flag": d.flag,
            "version": d.debian_version,
            "changes": d.changes,
        }
        for d in distros
    ]
    (build_dir / "distro_flags.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(f"### 打包結果：{package} {upstream_version}\n\n| 系列 | 版號 | 旗標 |\n|---|---|---|\n")
            for d in distros:
                fh.write(f"| {d.codename} ({d.ubuntu_version}) | `{d.debian_version}` | `{d.flag}` |\n")

    log("=== 所有選擇之發行版打包簽署完畢。 ===")


if __name__ == "__main__":
    main()
