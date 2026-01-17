
# 🛡️ Vaultwarden Extended (WebDAV 备份版)

**基于官方 Vaultwarden 构建，集成 WebDAV 自动加密备份与可视化管理面板。**

[![Docker Image](https://img.shields.io/badge/Docker-Image-blue?logo=docker)](https://ghcr.io/)
[![Python](https://img.shields.io/badge/Backend-FastAPI-green?logo=python)](https://fastapi.tiangolo.com/)
[![Vue3](https://img.shields.io/badge/Frontend-Vue3-emerald?logo=vue.js)](https://vuejs.org/)

这是一个 "All-in-One" 的 Docker 镜像方案。它在保持 [Vaultwarden](https://github.com/dani-garcia/vaultwarden)（原 Bitwarden_RS）官方核心功能原汁原味的同时，内置了一个轻量级的 **备份管理面板**。

解决了自建密码库最大的痛点：**数据安全与自动备份**。

---

## ✨ 核心功能

*   **🔒 官方同步**：基于 `vaultwarden/server:latest` 构建，核心服务与官方保持一致。
*   **📦 纯净备份**：采用 **"停止服务 -> 打包 -> 启动服务"** 的逻辑，确保 SQLite 数据库的绝对数据一致性。
*   **🔐 AES-256 加密**：备份文件为 `.zip` 格式，支持 AES-256 密码加密。
*   **☁️ WebDAV 上传**：支持将备份自动上传到坚果云、Nextcloud、Alist 等支持 WebDAV 的网盘。
*   **🖥️ 可视化面板**：
    *   独立的管理后台（默认端口 5000）。
    *   美观的 Vue3 + Element Plus 界面。
    *   支持在线配置 WebDAV、Cron 定时策略、通知渠道。
    *   支持从云端列表一键还原，或上传本地 `.zip` 文件还原。
*   **🤖 智能保留**：支持设置云端最大保留数量，自动清理旧备份。
*   **⏰ Cron 定时**：支持自定义 Cron 表达式。
*   **📢 多渠道通知**：支持 Telegram、Bark (iOS)、邮件通知，仅在备份/还原**失败**时发送。
*   **🌐 DDNSTO 内网穿透** (可选)：内置 DDNSTO 客户端，配置 Token 即可随时随地访问密码库。

---

## 🚀 快速部署 (Docker Compose)

这是最推荐的部署方式。

### 1. 准备工作
确保你的服务器已安装 Docker 和 Docker Compose。

### 2. 创建目录
在服务器上创建一个目录，例如 `vaultwarden`：
```bash
mkdir -p /root/vaultwarden/vw-data
mkdir -p /root/vaultwarden/vw-conf
cd /root/vaultwarden
```

### 3. 创建配置文件
创建 `docker-compose.yml` 文件：

```yaml
version: '3'

services:
  vaultwarden:
    # 请替换为你构建或拉取的实际镜像地址
    image: ghcr.io/你的用户名/vaultwarden-webdav:latest
    container_name: vaultwarden
    restart: always
    # 核心数据映射
    volumes:
      - ./vw-data:/data  # Vaultwarden 的核心数据
      - ./vw-conf:/conf  # 备份面板的配置文件和日志
    # 环境变量配置
    environment:
      - TZ=Asia/Shanghai            # 设置时区
      - WEBSOCKET_ENABLED=true      # 开启 WebSocket 支持
      - SIGNUPS_ALLOWED=false       # 禁止新用户注册
      
      # --- 面板登录账号设置 ---
      - DASHBOARD_ADMIN_USER=admin
      - DASHBOARD_ADMIN_PASSWORD=admin
      
      # --- DDNSTO 内网穿透 (可选) ---
      # - DDNSTO_TOKEN=你的令牌    # 从 ddnsto.com 获取，留空则不启用
    ports:
      - "8080:80"    # Vaultwarden 服务端口
      - "5000:5000"  # 备份管理面板端口
```

### 4. 启动服务
```bash
docker-compose up -d
```

---

## 📖 使用指南

### 1. 访问密码库
*   地址：`http://你的IP:8080`
*   这是标准的 Vaultwarden 界面，用于存储和管理密码。

### 2. 访问管理面板
*   地址：`http://你的IP:5000`
*   **登录**：输入你在 `docker-compose.yml` 中设置的 `DASHBOARD_ADMIN_USER` 和密码。

### 3. 配置自动备份
进入面板后，在 **"系统配置"** 卡片中填写：
1.  **WebDAV 设置**：填写你的网盘地址、账号密码。
2.  **WebDAV 存储路径**：例如 `/vaultwarden-backup`（程序会自动创建目录）。
3.  **备份加密密码**：强烈建议设置！设置后，生成的 `.zip` 文件将被 AES-256 加密。
4.  **Cron 表达式**：默认 `0 3 * * *`（每天凌晨 03:00）。
5.  **最大保留数量**：例如 `10`，超过数量会自动删除旧备份。
6.  **Telegram 通知**：填写 Bot Token 和 Chat ID（可选，仅失败时通知）。
7.  点击 **"保存所有配置"**。

### 4. 手动备份/还原
*   **立即备份**：点击左上角的 "立即备份到云端" 按钮。
*   **云端还原**：在右侧列表中找到历史备份，点击红色的 "下载还原" 按钮。
*   **本地还原**：点击 "上传 Zip 还原" 按钮，选择本地的备份文件进行恢复。

> ⚠️ **注意**：**还原操作是破坏性的！** 程序会先停止服务，**清空** `/data` 目录下的所有文件，然后解压备份包，最后重启服务。请谨慎操作。

---

## ⚙️ 详细配置参数

### 环境变量 (docker-compose.yml)

| 变量名 | 默认值 | 说明 |
| :--- | :--- | :--- |
| `TZ` | `Asia/Shanghai` | 容器时区，确保备份时间准确 |
| `DASHBOARD_ADMIN_USER` | `admin` | 管理面板的登录用户名 |
| `DASHBOARD_ADMIN_PASSWORD` | `admin` | 管理面板的登录密码 |
| `WEBSOCKET_ENABLED` | `true` | 是否启用 Vaultwarden 的 WebSocket |
| `SIGNUPS_ALLOWED` | `false` | 是否允许新用户注册 |
| `DDNSTO_TOKEN` | (空) | DDNSTO 令牌，留空则不启用内网穿透 |
| `DATA_FOLDER` | `/data` | **不要修改**，内部路径硬编码 |

### 路径映射 (Volumes)

| 宿主机路径 (示例) | 容器路径 | 说明 |
| :--- | :--- | :--- |
| `./vw-data` | `/data` | **核心数据**。包含 `db.sqlite3`、`attachments`、`rsa_keys` 等。备份程序只打包此目录。 |
| `./vw-conf` | `/conf` | **面板配置**。包含 `backup_config.json` (面板设置) 和 `manager.log` (运行日志)。 |

---

## 🛠️ 备份原理与安全性

为了保证数据的绝对安全，本项目采用以下备份逻辑：

1.  **Stop (停止)**: 暂停 Vaultwarden 主进程。这会强制 SQLite 将 WAL（预写日志）合并入主数据库文件，防止产生 "database disk image is malformed" 错误。
2.  **Pack (打包)**: 过滤掉系统垃圾文件（如 `.DS_Store`, `tmp`），将 `/data` 目录打包为 `.zip`。
3.  **Start (启动)**: 立即恢复 Vaultwarden 服务，将停机时间缩短到几秒钟。
4.  **Encrypt & Upload (加密上传)**: 在后台对 Zip 文件进行 AES-256 加密，并上传至 WebDAV 网盘。

---

## 🧑‍💻 开发者构建指南

如果你想自己修改代码并构建镜像：

1.  克隆仓库：
    ```bash
    git clone https://github.com/你的用户名/vaultwarden-webdav.git
    cd vaultwarden-webdav
    ```

2.  目录结构说明：
    *   `app/`: 包含 FastAPI 后端 (`main.py`) 和 Vue 前端 (`static/index.html`)。
    *   `conf/`: Supervisor 进程管理配置。
    *   `Dockerfile`: 构建文件。

3.  本地构建并运行：
    ```bash
    docker build -t my-vaultwarden .
    docker run -d -p 8080:80 -p 5000:5000 my-vaultwarden
    ```

---

## ❓ 常见问题 (FAQ)

**Q: 还原后提示 "Internal Server Error" 或数据库损坏？**
A: 本项目已针对此问题做了优化（还原前会自动清理 WAL 文件）。如果仍出现问题，请检查是否在其他地方手动替换了数据库文件但没清理 `.wal` 和 `.shm` 文件。

**Q: 下载的 Zip 文件怎么解压？**
A: 使用 WinRAR、7-Zip、Bandizip (Windows) 或 Keka (macOS) 等主流软件，双击打开，输入你在面板设置的 **"备份加密密码"** 即可。

**Q: 忘记了管理面板的密码怎么办？**
A: 修改 `docker-compose.yml` 中的环境变量 `DASHBOARD_ADMIN_PASSWORD`，然后重启容器 (`docker-compose up -d`) 即可生效。

---

## 📝 免责声明

本项目是基于 Vaultwarden 的第三方扩展，与 Bitwarden Inc. 或 Vaultwarden 官方团队无直接关联。使用本项目产生的任何数据丢失风险需自行承担。建议定期手动下载备份文件到本地进行多重存档。

---

**Made with ❤️ by [你的名字]**
