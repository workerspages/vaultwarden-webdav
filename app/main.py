import os
import shutil
# import tarfile  <-- 不再需要
import datetime
import logging
import json
import subprocess
import base64
import hashlib
import secrets
from typing import List, Dict, Optional

# 第三方库
import httpx
# import pyzipper  <-- 需要在这里导入
import pyzipper
from fastapi import FastAPI, UploadFile, BackgroundTasks, HTTPException, File, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from webdav4.client import Client as WebDavClient
# from cryptography.fernet import Fernet <-- 不再需要
from pytz import timezone

# --- 全局配置与常量 ---

DATA_DIR = "/data"
CONF_DIR = "/conf"
BACKUP_CONFIG_FILE = os.path.join(CONF_DIR, "backup_config.json")
LOG_FILE = os.path.join(CONF_DIR, "manager.log")
TEMP_DIR = "/tmp/backup_work"
TZ_CN = timezone('Asia/Shanghai')

# 环境变量
ADMIN_USER = os.getenv("DASHBOARD_ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("DASHBOARD_ADMIN_PASSWORD", "admin")

os.makedirs(CONF_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

# 日志配置
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(console_handler)

app = FastAPI(title="Vaultwarden Dashboard")
security = HTTPBasic()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 鉴权 ---
def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    is_user_correct = secrets.compare_digest(credentials.username, ADMIN_USER)
    is_pass_correct = secrets.compare_digest(credentials.password, ADMIN_PASS)
    if not (is_user_correct and is_pass_correct):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# --- 辅助函数 ---
def load_config() -> dict:
    if os.path.exists(BACKUP_CONFIG_FILE):
        try:
            with open(BACKUP_CONFIG_FILE, 'r', encoding='utf-8') as f: return json.load(f)
        except: pass
    return {}

def save_config(config: dict):
    with open(BACKUP_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    try: schedule_backup_job(config)
    except: pass

def get_current_time_str():
    return datetime.datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")

def send_telegram_notify(msg: str, success: bool = True):
    cfg = load_config()
    token = cfg.get("tg_bot_token")
    chat_id = cfg.get("tg_chat_id")
    if not token or not chat_id: return
    emoji = "✅" if success else "❌"
    title = "Vaultwarden 备份成功" if success else "Vaultwarden 备份/还原失败"
    text = f"{emoji} *{title}*\n\n{msg}\n\n🕒 时间: {datetime.datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')}"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try: httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except: pass

# --- 服务控制 ---
def stop_service():
    logging.info("停止服务...")
    subprocess.run(["supervisorctl", "stop", "vaultwarden"], check=True)

def start_service():
    logging.info("启动服务...")
    subprocess.run(["supervisorctl", "start", "vaultwarden"], check=True)

# --- 过滤器逻辑 ---
def is_file_allowed(filename: str) -> bool:
    """检查文件是否允许备份"""
    if filename in ['lost+found', '.DS_Store', 'Thumbs.db']: return False
    if filename.endswith('.bak') or filename.endswith('.tmp') or filename.endswith('.swp'): return False
    return True

# --- 保留策略 ---
def apply_retention_policy(client: WebDavClient, remote_dir: str):
    cfg = load_config()
    max_backups = int(cfg.get("max_backups", 10))
    if max_backups < 1: max_backups = 10
    try:
        files = client.ls(remote_dir, detail=True)
        backups = []
        for f in files:
            if f['type'] == 'directory': continue
            name = os.path.basename(f['name'])
            if "vw_backup_" in name:
                backups.append({"name": name, "path": f['name'], "sort_key": name})
        backups.sort(key=lambda x: x['sort_key'], reverse=True)
        if len(backups) > max_backups:
            for item in backups[max_backups:]:
                try: client.remove(item['path'])
                except: pass
    except: pass

# --- 核心备份逻辑 (Zip 版) ---
def perform_backup():
    logging.info(">>> 开始备份 (Zip AES-256)")
    cfg = load_config()
    if not cfg.get("webdav_url"):
        logging.warning("未配置 WebDAV")
        return

    tmp_files = []
    try:
        timestamp = get_current_time_str()
        # 改为 .zip 后缀
        backup_name = f"vw_backup_{timestamp}.zip"
        zip_path = os.path.join(TEMP_DIR, backup_name)
        tmp_files.append(zip_path)

        # 1. 停止服务
        try: stop_service()
        except: return

        # 2. 创建加密 Zip
        try:
            logging.info(f"正在打包并加密到 {zip_path} ...")
            password = cfg.get("encryption_password")
            
            # 使用 pyzipper 创建 AES 加密包
            # compression=pyzipper.ZIP_DEFLATED (标准压缩)
            # encryption=pyzipper.WZ_AES (WinZip AES 标准，兼容性好)
            with pyzipper.AESZipFile(zip_path, 'w', compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES) as zf:
                if password:
                    zf.setpassword(password.encode('utf-8'))
                    zf.setencryption(pyzipper.WZ_AES, nbits=256) # 256位高强度加密
                
                # 遍历 /data 目录
                for root, dirs, files in os.walk(DATA_DIR):
                    for file in files:
                        if is_file_allowed(file):
                            abs_path = os.path.join(root, file)
                            # 计算相对路径，确保解压时路径正确 (例如: db.sqlite3, attachments/xxx)
                            rel_path = os.path.relpath(abs_path, DATA_DIR)
                            zf.write(abs_path, arcname=rel_path)
                            
        except Exception as e:
            logging.error(f"打包失败: {e}")
            start_service()
            raise e
        
        # 3. 恢复服务
        try: start_service()
        except Exception as e:
            send_telegram_notify("严重错误: 备份后服务无法启动", success=False)
            raise e

        # 4. 上传 (Zip本身已加密，无需额外加密步骤)
        logging.info("正在上传...")
        client = WebDavClient(cfg["webdav_url"], auth=(cfg.get("webdav_user", ""), cfg.get("webdav_password", "")))
        remote_dir = cfg.get('webdav_path', '/')
        try:
            if remote_dir != "/" and not client.exists(remote_dir): client.mkdir(remote_dir)
        except: pass

        remote_path = f"{remote_dir}/{backup_name}".replace("//", "/")
        client.upload_file(zip_path, remote_path)
        logging.info("上传成功")
        
        apply_retention_policy(client, remote_dir)
        logging.info(f"备份完成: {backup_name}")

    except Exception as e:
        logging.error(f"备份异常: {e}", exc_info=True)
        send_telegram_notify(f"备份失败: {str(e)}", success=False)
    finally:
        for f in tmp_files:
            if os.path.exists(f): 
                try: os.remove(f)
                except: pass

# --- 还原逻辑 (Zip 版) ---
def process_restore_file(local_file_path: str):
    logging.info(">>> 开始还原 (Zip)")
    cfg = load_config()
    
    try:
        # 1. 验证 Zip
        if not pyzipper.is_zipfile(local_file_path):
            raise ValueError("不是有效的 Zip 文件")

        # 2. 停止服务
        stop_service()

        # 3. 清空数据目录
        logging.info("清空数据目录...")
        for filename in os.listdir(DATA_DIR):
            file_path = os.path.join(DATA_DIR, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path): os.unlink(file_path)
                elif os.path.isdir(file_path): shutil.rmtree(file_path)
            except: pass

        # 4. 解压
        logging.info("正在解压...")
        password = cfg.get("encryption_password")
        
        with pyzipper.AESZipFile(local_file_path, 'r') as zf:
            if password:
                zf.setpassword(password.encode('utf-8'))
            # extractall 会自动处理目录结构
            zf.extractall(path=DATA_DIR)
        
        # 5. 启动服务
        start_service()
        logging.info("还原成功")

    except Exception as e:
        logging.error(f"还原失败: {e}", exc_info=True)
        send_telegram_notify(f"还原失败: {str(e)}", success=False)
        try: start_service() 
        except: pass
    finally:
        if os.path.exists(local_file_path): 
            try: os.remove(local_file_path)
            except: pass

def download_and_restore(filename: str):
    cfg = load_config()
    local_filename = os.path.basename(filename)
    local_path = os.path.join(TEMP_DIR, local_filename)
    try:
        logging.info(f"下载: {filename}")
        client = WebDavClient(cfg["webdav_url"], auth=(cfg.get("webdav_user", ""), cfg.get("webdav_password", "")))
        remote_path = f"{cfg.get('webdav_path', '/')}/{local_filename}".replace("//", "/")
        client.download_file(remote_path, local_path)
        process_restore_file(local_path)
    except Exception as e:
        logging.error(f"下载/还原出错: {e}")
        send_telegram_notify(f"出错: {e}", success=False)

# --- 调度与API (基本不变，只需注意 update_config) ---
scheduler = BackgroundScheduler(timezone=TZ_CN)
def schedule_backup_job(config: dict):
    if scheduler.get_job('backup_job'): scheduler.remove_job('backup_job')
    cron_exp = config.get('schedule_cron', '0 3 * * *')
    try:
        trigger = CronTrigger.from_crontab(cron_exp, timezone=TZ_CN)
        scheduler.add_job(perform_backup, trigger, id='backup_job', replace_existing=True)
        logging.info(f"调度: {cron_exp}")
    except:
        scheduler.add_job(perform_backup, CronTrigger(hour=3, minute=0, timezone=TZ_CN), id='backup_job', replace_existing=True)

scheduler.start()
initial_cfg = load_config()
schedule_backup_job(initial_cfg)

@app.get("/", response_class=HTMLResponse)
async def read_root():
    index_path = "/app/static/index.html"
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f: return f.read()
    return "UI File Not Found."

@app.get("/api/auth_check", dependencies=[Depends(check_auth)])
async def auth_check(): return {"status": "authenticated"}

@app.get("/api/config", dependencies=[Depends(check_auth)])
async def get_config(): return load_config()

@app.post("/api/config", dependencies=[Depends(check_auth)])
async def update_config(config: dict):
    save_config(config)
    return {"status": "success"}

@app.post("/api/backup/now", dependencies=[Depends(check_auth)])
async def trigger_backup_manual(background_tasks: BackgroundTasks):
    background_tasks.add_task(perform_backup)
    return {"status": "started"}

@app.get("/api/backups", dependencies=[Depends(check_auth)])
async def list_backups():
    cfg = load_config()
    if not cfg.get("webdav_url"): return JSONResponse(status_code=400, content={"error": "WebDAV not configured"})
    try:
        client = WebDavClient(cfg["webdav_url"], auth=(cfg.get("webdav_user", ""), cfg.get("webdav_password", "")))
        files = client.ls(cfg.get('webdav_path', '/'), detail=True)
        backup_files = []
        for f in files:
            if f.get('type') != 'directory' and "vw_backup_" in f.get('name', ''):
                clean_name = os.path.basename(f['name'])
                raw_size = f.get('size') if f.get('size') is not None else f.get('content_length')
                try: size_bytes = int(raw_size) if raw_size is not None else 0
                except: size_bytes = 0
                size_mb = round(size_bytes / 1024 / 1024, 2)
                backup_files.append({"name": clean_name, "size": f"{size_mb} MB", "last_modified": f.get('last_modified', '')})
        return sorted(backup_files, key=lambda x: x['name'], reverse=True)
    except Exception as e: return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/restore", dependencies=[Depends(check_auth)])
async def restore_from_cloud(file_name: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(download_and_restore, file_name)
    return {"status": "started"}

@app.post("/api/upload_restore", dependencies=[Depends(check_auth)])
async def upload_and_restore(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    local_path = os.path.join(TEMP_DIR, file.filename)
    try:
        with open(local_path, "wb") as buffer: shutil.copyfileobj(file.file, buffer)
        background_tasks.add_task(process_restore_file, local_path)
        return {"status": "started"}
    except Exception as e: return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/logs", dependencies=[Depends(check_auth)])
async def get_logs():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'r', encoding='utf-8') as f: return {"logs": "".join(f.readlines()[-100:])}
        except: pass
    return {"logs": "No logs yet."}
