# fcitx5-mcbopomofo (小麥注音) PPA for Ubuntu

本 PPA 提供官方套件庫尚未收錄的 **fcitx5-mcbopomofo (McBopomofo / 小麥注音)** 最新穩定編譯版本。

[![Resolute](https://img.shields.io/badge/dynamic/json?label=Resolute&query=%24.entries%5B0%5D.binary_package_version&url=https%3A%2F%2Fapi.launchpad.net%2F1.0%2F%7Ealvin99%2F%2Barchive%2Fubuntu%2Ffcitx5-mcbopomofo%3Fws.op%3DgetPublishedBinaries%26binary_name%3Dfcitx5-mcbopomofo%26exact_match%3Dtrue%26distro_series%3Dhttps%253A%252F%252Fapi.launchpad.net%252F1.0%252Fubuntu%252Fresolute&color=orange&logo=launchpad)](https://launchpad.net/~alvin99/+archive/ubuntu/fcitx5-mcbopomofo)
[![Noble](https://img.shields.io/badge/dynamic/json?label=Noble&query=%24.entries%5B0%5D.binary_package_version&url=https%3A%2F%2Fapi.launchpad.net%2F1.0%2F%7Ealvin99%2F%2Barchive%2Fubuntu%2Ffcitx5-mcbopomofo%3Fws.op%3DgetPublishedBinaries%26binary_name%3Dfcitx5-mcbopomofo%26exact_match%3Dtrue%26distro_series%3Dhttps%253A%252F%252Fapi.launchpad.net%252F1.0%252Fubuntu%252Fnoble&color=orange&logo=launchpad)](https://launchpad.net/~alvin99/+archive/ubuntu/fcitx5-mcbopomofo)
[![Jammy](https://img.shields.io/badge/dynamic/json?label=Jammy&query=%24.entries%5B0%5D.binary_package_version&url=https%3A%2F%2Fapi.launchpad.net%2F1.0%2F%7Ealvin99%2F%2Barchive%2Fubuntu%2Ffcitx5-mcbopomofo%3Fws.op%3DgetPublishedBinaries%26binary_name%3Dfcitx5-mcbopomofo%26exact_match%3Dtrue%26distro_series%3Dhttps%253A%252F%252Fapi.launchpad.net%252F1.0%252Fubuntu%252Fjammy&color=orange&logo=launchpad)](https://launchpad.net/~alvin99/+archive/ubuntu/fcitx5-mcbopomofo)
[![Upload PPA](https://img.shields.io/github/actions/workflow/status/Alvin82/fcitx5-mcbopomofo-ubuntu/ppa-build.yaml?label=Upload%20PPA&logo=githubactions)](https://github.com/Alvin82/fcitx5-mcbopomofo-ubuntu/actions)

---

### 🚀 支援的 Ubuntu 版本 (Supported Releases)

| Ubuntu 版本 | 代號 (Codename) | 支援架構 (Architectures) |
| :--- | :--- | :--- |
| **Ubuntu 26.04 LTS** | Resolute Raccoon | amd64 / arm64 |
| **Ubuntu 24.04 LTS** | Noble Numbat | amd64 / arm64 |
| **Ubuntu 22.04 LTS** | Jammy Jellyfish | amd64 / arm64 |

---

### 📦 快速安裝指令 (Quick Installation)

打開終端機 (Terminal) 並依序執行以下指令：

```bash
sudo add-apt-repository ppa:alvin99/fcitx5-mcbopomofo
sudo apt update
sudo apt install fcitx5-mcbopomofo
```

---

### ⚙️ 輸入法框架切換 (Input Method Framework Selection)

確保系統預設輸入法框架已從 IBus 切換為 Fcitx5（建議於個人帳號執行一次即可）：

```bash
im-config -n fcitx5
```

---

### 🛠️ 環境變數配置 (Systemd Environment.d Configuration)

> [!TIP]
> 雖然 Ubuntu 在安裝 fcitx5 後會嘗試動態整合，但在 Wayland 桌面環境下，Chrome、Discord、VS Code 等視窗應用程式仍常因讀不到變數而無法切換輸入法。
> 
> **若您遇到此痛點，請依權限需求擇一配置以下環境變數（設定完成後請「登出並重新登入」或「重新啟動系統」）：**

#### 💡 方案 A：個人帳號層級設定 (Per-user)
*無需 sudo 管理員權限，僅影響當前單一使用者。直接複製並在終端機執行以下整段指令：*

```bash
mkdir -p ~/.config/environment.d
tee ~/.config/environment.d/fcitx5.conf << 'EOF'
GTK_IM_MODULE=fcitx
QT_IM_MODULE=fcitx
XMODIFIERS=@im=fcitx
SDL_IM_MODULE=fcitx
GLFW_IM_MODULE=fcitx
EOF
```

#### 💡 方案 B：全域系統層級設定 (System-wide)
*適用於系統管理員，自動套用到所有使用者。直接複製並在終端機執行以下整段指令：*

```bash
sudo mkdir -p /etc/environment.d
sudo tee /etc/environment.d/99-fcitx5.conf << 'EOF'
GTK_IM_MODULE=fcitx
QT_IM_MODULE=fcitx
XMODIFIERS=@im=fcitx
SDL_IM_MODULE=fcitx
GLFW_IM_MODULE=fcitx
EOF
```

---

### 🖥️ 應用程式相容性優化 (Chromium & Electron Applications)

Chromium / Electron 核心軟體（如 VS Code、Slack、Obsidian）若在 Wayland 桌面環境下出現漏字或無法呼叫候選框的問題，請在軟體啟動參數中加入以下旗標：

```text
--ozone-platform-hint=auto --im-module=fcitx5
```

---

### 🌐 相關連結 (Links)

* **Launchpad PPA:** [ppa:alvin99/fcitx5-mcbopomofo](https://launchpad.net/~alvin99/+archive/ubuntu/fcitx5-mcbopomofo)
* **上游專案源碼與回報:** [openvanilla/fcitx5-mcbopomofo](https://github.com/openvanilla/fcitx5-mcbopomofo)
