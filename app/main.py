import os
import shutil
import tarfile
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
from fastapi import FastAPI, UploadFile, BackgroundTasks, HTTPException, File, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.middleware.cors import CORSMiddleware
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from webdav4.client import Client as WebDavClient
from cryptography.fernet import Fernet
from pytz import timezone

# --- 全局配置与常量 ---

DATA_DIR = "/data"
CONF_DIR = "/conf"
BACKUP_CONFIG_FILE = os.path.join(CONF_DIR, "backup_config.json")
LOG_FILE = os.path.join(CONF_DIR, "manager.log")
TEMP_DIR = "/tmp/backup_work"
TZ_CN = timezone('Asia/Shanghai')

# 读取环境变量中的管理员账号密码
ADMIN_USER = os.getenv("DASHBOARD_ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("DASHBOARD_ADMIN_PASSWORD", "admin")

# 确保必要目录存在
os.makedirs(CONF_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)
# 确保数据目录存在（防止首次启动报错）
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

# --- 鉴权函数 ---

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

# --- 辅助功能函数 ---

def load_config() -> dict:
    if os.path.exists(BACKUP_CONFIG_FILE):
        try:
            with open(BACKUP_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"加载配置失败: {e}")
    return {}

def save_config(config: dict):
    with open(BACKUP_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    try:
        schedule_backup_job(config)
    except Exception as e:
        logging.error(f"更新调度任务失败: {e}")

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
    try:
        httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logging.error(f"Telegram 发送失败: {e}")

def get_fernet_key(password: str) -> bytes:
    digest = hashlib.sha256(password.encode()).digest()
    return base64.urlsafe_b64encode(digest)

def encrypt_file(file_path: str, password: str) -> str:
    key = get_fernet_key(password)
    fernet = Fernet(key)
    with open(file_path, 'rb') as f: data = f.read()
    encrypted_data = fernet.encrypt(data)
    out_path = file_path + ".enc"
    with open(out_path, 'wb') as f: f.write(encrypted_data)
    return out_path

def decrypt_file(file_path: str, password: str) -> str:
    key = get_fernet_key(password)
    fernet = Fernet(key)
    with open(file_path, 'rb') as f: data = f.read()
    decrypted_data = fernet.decrypt(data)
    out_path = file_path.replace(".enc", "")
    with open(out_path, 'wb') as f: f.write(decrypted_data)
    return out_path

# --- 服务控制函数 ---

def stop_service():
    logging.info("正在停止 Vaultwarden 服务...")
    subprocess.run(["supervisorctl", "stop", "vaultwarden"], check=True)

def start_service():
    logging.info("正在启动 Vaultwarden 服务...")
    subprocess.run(["supervisorctl", "start", "vaultwarden"], check=True)

# --- 保留策略逻辑 ---

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
            to_delete = backups[max_backups:]
            for item in to_delete:
                path_to_remove = item['path']
                try:
                    client.remove(path_to_remove)
                    logging.info(f"保留策略删除: {path_to_remove}")
                except Exception as ex:
                    # 路径兼容重试
                    try:
                        if not path_to_remove.startswith('/'): client.remove('/' + path_to_remove)
                        else: client.remove(path_to_remove.lstrip('/'))
                    except: pass
    except Exception as e:
        logging.error(f"保留策略出错: {e}")

# --- 核心备份逻辑 ---

def perform_backup():
    logging.info(">>> 开始执行备份任务")
    cfg = load_config()
    if not cfg.get("webdav_url"):
        logging.warning("未配置 WebDAV，跳过备份")
        return

    tmp_files = []
    backup_name = ""

    try:
        timestamp = get_current_time_str()
        backup_name = f"vw_backup_{timestamp}.tar.gz"
        tar_path = os.path.join(TEMP_DIR, backup_name)
        tmp_files.append(tar_path)

        # 1. 停止服务（确保数据一致性）
        try:
            stop_service()
        except Exception as e:
            logging.error(f"停止服务失败，中止备份: {e}")
            return

        # 2. 打包整个 /data 目录
        try:
            logging.info(f"正在打包 {DATA_DIR} 目录...")
            with tarfile.open(tar_path, "w:gz") as tar:
                # arcname="" 表示将 /data 下的内容直接放在压缩包根目录
                # 这样解压时直接解压到 /data 即可
                tar.add(DATA_DIR, arcname="")
        except Exception as e:
            logging.error(f"打包失败: {e}")
            start_service() # 尝试恢复服务
            raise e
        
        # 3. 立即恢复服务（减少停机时间）
        try:
            start_service()
        except Exception as e:
            logging.error(f"服务启动失败! 请手动检查: {e}")
            send_telegram_notify("严重错误：备份后服务无法自动启动！", success=False)
            raise e

        # 4. 加密 (耗时操作放在服务启动后)
        upload_path = tar_path
        if cfg.get("encryption_password"):
            logging.info("正在加密备份文件...")
            upload_path = encrypt_file(tar_path, cfg["encryption_password"])
            tmp_files.append(upload_path)
            backup_name += ".enc"

        # 5. 上传
        logging.info("正在上传到 WebDAV...")
        client = WebDavClient(cfg["webdav_url"], auth=(cfg.get("webdav_user", ""), cfg.get("webdav_password", "")))
        remote_dir = cfg.get('webdav_path', '/')
        try:
            if remote_dir != "/" and not client.exists(remote_dir): client.mkdir(remote_dir)
        except: pass

        remote_path = f"{remote_dir}/{backup_name}".replace("//", "/")
        client.upload_file(upload_path, remote_path)
        logging.info("上传成功")
        
        # 6. 保留策略
        apply_retention_policy(client, remote_dir)
        logging.info(f"备份流程结束: {backup_name}")

    except Exception as e:
        logging.error(f"备份流程异常: {e}", exc_info=True)
        send_telegram_notify(f"备份失败: {str(e)}", success=False)
    finally:
        # 清理临时文件
        for f in tmp_files:
            if os.path.exists(f):
                try: os.remove(f)
                except: pass

# --- 还原逻辑 ---

def process_restore_file(local_file_path: str):
    logging.info(">>> 开始执行还原任务")
    cfg = load_config()
    temp_restored_files = []
    
    try:
        work_file = local_file_path

        # 1. 解密
        if local_file_path.endswith(".enc"):
            if not cfg.get("encryption_password"):
                raise ValueError("文件已加密，但未配置解密密码！")
            logging.info("正在解密文件...")
            work_file = decrypt_file(local_file_path, cfg["encryption_password"])
            temp_restored_files.append(work_file)
        
        # 2. 校验
        if not tarfile.is_tarfile(work_file):
            raise ValueError("无效的备份文件")

        # 3. 停止服务
        try:
            stop_service()
        except Exception as e:
            logging.error(f"停止服务失败: {e}")
            raise e

        # 4. 清空 /data 目录 (危险操作，需谨慎)
        logging.info(f"正在清空数据目录 {DATA_DIR} ...")
        for filename in os.listdir(DATA_DIR):
            file_path = os.path.join(DATA_DIR, filename)
            try:
                if os.path.isfile(file_path) or os.path.islink(file_path):
                    os.unlink(file_path)
                elif os.path.isdir(file_path):
                    shutil.rmtree(file_path)
            except Exception as e:
                logging.error(f"删除 {file_path} 失败: {e}")

        # 5. 解压还原
        logging.info("正在解压备份文件...")
        with tarfile.open(work_file, "r:gz") as tar:
            # 这里的解压路径直接是 DATA_DIR
            tar.extractall(path=DATA_DIR)
        
        logging.info("数据目录已替换完成")

        # 6. 启动服务
        try:
            start_service()
            logging.info("还原完成，服务已重启")
        except Exception as e:
            logging.error(f"服务启动失败: {e}")
            raise e

    except Exception as e:
        logging.error(f"还原失败: {e}", exc_info=True)
        send_telegram_notify(f"还原失败: {str(e)}", success=False)
        # 尝试保底启动
        try: start_service() 
        except: pass
    finally:
        if os.path.exists(local_file_path): os.remove(local_file_path)
        for f in temp_restored_files:
            if os.path.exists(f): os.remove(f)

def download_and_restore(filename: str):
    cfg = load_config()
    local_filename = os.path.basename(filename)
    local_path = os.path.join(TEMP_DIR, local_filename)
    try:
        logging.info(f"开始下载: {filename}")
        client = WebDavClient(cfg["webdav_url"], auth=(cfg.get("webdav_user", ""), cfg.get("webdav_password", "")))
        remote_path = f"{cfg.get('webdav_path', '/')}/{local_filename}".replace("//", "/")
        client.download_file(remote_path, local_path)
        process_restore_file(local_path)
    except Exception as e:
        logging.error(f"下载/还原出错: {e}")
        send_telegram_notify(f"下载/还原出错: {e}", success=False)

# --- 调度器设置 ---

scheduler = BackgroundScheduler(timezone=TZ_CN)
def schedule_backup_job(config: dict):
    if scheduler.get_job('backup_job'): scheduler.remove_job('backup_job')
    cron_exp = config.get('schedule_cron', '0 3 * * *')
    try:
        trigger = CronTrigger.from_crontab(cron_exp, timezone=TZ_CN)
        scheduler.add_job(perform_backup, trigger, id='backup_job', replace_existing=True)
        logging.info(f"调度更新: {cron_exp}")
    except:
        scheduler.add_job(perform_backup, CronTrigger(hour=3, minute=0, timezone=TZ_CN), id='backup_job', replace_existing=True)

scheduler.start()
initial_cfg = load_config()
schedule_backup_job(initial_cfg)

# --- API 路由 ---

@app.get("/", response_class=HTMLResponse)
async def read_root():
    index_path = "/app/static/index.html"
    if not os.path.exists(index_path): index_path = "app/static/index.html"
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
