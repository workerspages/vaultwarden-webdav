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

# 读取环境变量中的管理员账号密码，默认为 admin/admin
ADMIN_USER = os.getenv("DASHBOARD_ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("DASHBOARD_ADMIN_PASSWORD", "admin")

# 确保必要目录存在
os.makedirs(CONF_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# 日志配置
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
# 同时输出到控制台以便 docker logs 查看
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
logging.getLogger().addHandler(console_handler)

app = FastAPI(title="Vaultwarden Dashboard")
security = HTTPBasic()

# 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- 鉴权函数 ---

def check_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """验证用户名和密码"""
    # 使用 secrets.compare_digest 防止时序攻击
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
    """加载配置文件"""
    if os.path.exists(BACKUP_CONFIG_FILE):
        try:
            with open(BACKUP_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"加载配置失败: {e}")
    return {}

def save_config(config: dict):
    """保存配置文件，并尝试更新调度任务"""
    with open(BACKUP_CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
    
    # 尝试重新调度
    try:
        schedule_backup_job(config)
    except Exception as e:
        logging.error(f"更新调度任务失败: {e}")

def get_current_time_str():
    """获取当前北京时间字符串"""
    return datetime.datetime.now(TZ_CN).strftime("%Y%m%d_%H%M%S")

def send_telegram_notify(msg: str, success: bool = True):
    """发送 Telegram 通知"""
    cfg = load_config()
    token = cfg.get("tg_bot_token")
    chat_id = cfg.get("tg_chat_id")
    
    if not token or not chat_id:
        return
    
    emoji = "✅" if success else "❌"
    title = "Vaultwarden 备份成功" if success else "Vaultwarden 备份/还原失败"
    text = f"{emoji} *{title}*\n\n{msg}\n\n🕒 时间: {datetime.datetime.now(TZ_CN).strftime('%Y-%m-%d %H:%M:%S')}"
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        httpx.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
    except Exception as e:
        logging.error(f"Telegram 发送失败: {e}")

def get_fernet_key(password: str) -> bytes:
    """根据密码生成固定的 AES Key (使用 SHA256 派生)"""
    digest = hashlib.sha256(password.encode()).digest()
    return base64.urlsafe_b64encode(digest)

def encrypt_file(file_path: str, password: str) -> str:
    """加密文件，返回新路径 (.enc)"""
    key = get_fernet_key(password)
    fernet = Fernet(key)
    
    with open(file_path, 'rb') as f:
        data = f.read()
    
    encrypted_data = fernet.encrypt(data)
    out_path = file_path + ".enc"
    
    with open(out_path, 'wb') as f:
        f.write(encrypted_data)
        
    return out_path

def decrypt_file(file_path: str, password: str) -> str:
    """解密文件，返回去除了 .enc 的路径"""
    key = get_fernet_key(password)
    fernet = Fernet(key)
    
    with open(file_path, 'rb') as f:
        data = f.read()
    
    decrypted_data = fernet.decrypt(data)
    out_path = file_path.replace(".enc", "")
    
    with open(out_path, 'wb') as f:
        f.write(decrypted_data)
        
    return out_path

# --- GFS (Grandfather-Father-Son) 保留策略逻辑 ---

def parse_backup_date(filename: str) -> Optional[datetime.datetime]:
    """从文件名解析日期 vw_backup_YYYYMMDD_HHMMSS"""
    try:
        # 假设格式如: vw_backup_20231001_120000.tar.gz.enc
        base = filename.split('.')[0] # 去掉后缀
        # 提取 vw_backup_ 后面的部分
        parts = base.split('_')
        if len(parts) >= 3:
            date_str = f"{parts[2]}_{parts[3]}"
            dt = datetime.datetime.strptime(date_str, "%Y%m%d_%H%M%S")
            return dt.replace(tzinfo=TZ_CN)
    except Exception:
        return None
    return None

def apply_retention_policy(client: WebDavClient, remote_dir: str):
    """应用 GFS 策略清理旧备份"""
    try:
        # 注意：webdav4 的 ls 默认可能只返回名字，必须加 detail=True 才能获取完整字典信息
        files = client.ls(remote_dir, detail=True)
        backups = []
        
        # 筛选并解析备份文件
        for f in files:
            # f 是一个字典，包含 name, size, type 等
            if f['type'] == 'directory':
                continue
                
            name = f['name']
            if "vw_backup_" in name:
                dt = parse_backup_date(name)
                if dt:
                    backups.append({"name": name, "dt": dt, "path": name})
        
        # 按时间倒序排列（最新的在前面）
        backups.sort(key=lambda x: x['dt'], reverse=True)
        
        if not backups:
            return

        now = datetime.datetime.now(TZ_CN)
        to_keep = set()
        to_delete = set()

        # 策略定义
        keep_days = 7
        keep_weeks = 4
        keep_months = 12

        # 1. 保留最近 7 天的所有备份
        for b in backups:
            if (now - b['dt']).days < keep_days:
                to_keep.add(b['name'])

        # 2. 保留最近 4 周（每周一份，取该周最新的）
        for i in range(keep_weeks):
            start_window = now - datetime.timedelta(weeks=i+1)
            end_window = now - datetime.timedelta(weeks=i)
            candidates = [b for b in backups if start_window <= b['dt'] < end_window]
            if candidates:
                to_keep.add(candidates[0]['name'])

        # 3. 保留最近 12 个月（每月一份，取该月最新的）
        for i in range(keep_months):
            start_window = now - datetime.timedelta(days=(i+1)*30)
            end_window = now - datetime.timedelta(days=i*30)
            candidates = [b for b in backups if start_window <= b['dt'] < end_window]
            if candidates:
                to_keep.add(candidates[0]['name'])

        # 标记删除
        for b in backups:
            if b['name'] not in to_keep:
                to_delete.add(b['name'])

        # 执行删除
        for name in to_delete:
            full_path = f"{remote_dir}/{name}".replace("//", "/")
            logging.info(f"GFS 策略清理: 删除 {name}")
            client.remove(full_path)
            
    except Exception as e:
        logging.error(f"GFS 清理过程出错: {e}")

# --- 核心备份逻辑 ---

def perform_backup():
    """执行完整的备份流程"""
    logging.info("开始执行定时备份任务...")
    cfg = load_config()
    
    if not cfg.get("webdav_url"):
        logging.warning("未配置 WebDAV，跳过备份。")
        return

    tmp_files = []
    backup_name = ""

    try:
        timestamp = get_current_time_str()
        backup_name = f"vw_backup_{timestamp}.tar.gz"
        tar_path = os.path.join(TEMP_DIR, backup_name)
        tmp_files.append(tar_path)

        # 1. 备份 SQLite 数据库
        sqlite_db_path = os.path.join(DATA_DIR, "db.sqlite3")
        backup_db_path = os.path.join(TEMP_DIR, "db.sqlite3")
        
        if os.path.exists(sqlite_db_path):
            logging.info("正在导出 SQLite 数据库...")
            subprocess.run(["sqlite3", sqlite_db_path, f".backup '{backup_db_path}'"], check=True)
            tmp_files.append(backup_db_path)
        else:
            logging.warning("未找到 db.sqlite3，可能是首次运行。")

        # 2. 打包文件
        logging.info("正在打包文件...")
        with tarfile.open(tar_path, "w:gz") as tar:
            if os.path.exists(backup_db_path):
                tar.add(backup_db_path, arcname="db.sqlite3")
            
            for item in ["attachments", "sends", "rsa_key.pem", "rsa_key.pub.pem", "config.json", "data.json", "icon_cache"]:
                p = os.path.join(DATA_DIR, item)
                if os.path.exists(p):
                    tar.add(p, arcname=item)
        
        # 3. 加密
        upload_path = tar_path
        if cfg.get("encryption_password"):
            logging.info("正在加密备份文件...")
            upload_path = encrypt_file(tar_path, cfg["encryption_password"])
            tmp_files.append(upload_path)
            backup_name += ".enc"

        # 4. 上传到 WebDAV
        logging.info(f"正在上传到 WebDAV: {cfg['webdav_url']}")
        client = WebDavClient(
            cfg["webdav_url"], 
            auth=(cfg.get("webdav_user", ""), cfg.get("webdav_password", ""))
        )
        
        remote_dir = cfg.get('webdav_path', '/')
        # 确保远程目录存在
        try:
            if remote_dir != "/":
                if not client.exists(remote_dir):
                    client.mkdir(remote_dir)
        except Exception as e:
             logging.warning(f"尝试创建目录失败(可能已存在或权限不足): {e}")

        remote_path = f"{remote_dir}/{backup_name}".replace("//", "/")
        
        # 【修正】webdav4 使用 upload_file 方法
        client.upload_file(upload_path, remote_path)
        logging.info("上传成功。")
        
        # 5. 执行 GFS 保留策略
        logging.info("正在执行 GFS 保留策略...")
        apply_retention_policy(client, remote_dir)

        send_telegram_notify(f"备份文件已上传: {backup_name}\nGFS 策略检查完成。")

    except Exception as e:
        logging.error(f"备份流程失败: {e}", exc_info=True)
        send_telegram_notify(f"备份流程发生异常: {str(e)}", success=False)
    finally:
        for f in tmp_files:
            if os.path.exists(f):
                try:
                    if os.path.isdir(f): shutil.rmtree(f)
                    else: os.remove(f)
                except:
                    pass

# --- 还原逻辑 ---

def restart_vaultwarden():
    """重启 Vaultwarden 进程"""
    logging.info("正在重启 Vaultwarden...")
    try:
        subprocess.run(["supervisorctl", "restart", "vaultwarden"], check=True)
        logging.info("Vaultwarden 重启命令已发送。")
    except subprocess.CalledProcessError as e:
        logging.error(f"重启 Vaultwarden 失败: {e}")
        raise e

def process_restore_file(local_file_path: str):
    """处理还原文件的核心逻辑：解密 -> 解压 -> 覆盖 -> 重启"""
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
        
        # 2. 解压并覆盖
        logging.info("正在解压覆盖数据...")
        if not tarfile.is_tarfile(work_file):
            raise ValueError("文件不是有效的 tar 归档")

        subprocess.run(["supervisorctl", "stop", "vaultwarden"], check=False)

        with tarfile.open(work_file, "r:gz") as tar:
            tar.extractall(path=DATA_DIR)
        
        logging.info("数据覆盖完成。")

        # 3. 重启服务
        restart_vaultwarden()
        send_telegram_notify("系统已成功从备份还原并重启。")

    except Exception as e:
        logging.error(f"还原失败: {e}", exc_info=True)
        send_telegram_notify(f"还原操作失败: {str(e)}", success=False)
        subprocess.run(["supervisorctl", "start", "vaultwarden"], check=False)
    finally:
        if os.path.exists(local_file_path):
            os.remove(local_file_path)
        for f in temp_restored_files:
            if os.path.exists(f):
                os.remove(f)

def download_and_restore(filename: str):
    """后台任务：从 WebDAV 下载并还原"""
    cfg = load_config()
    local_path = os.path.join(TEMP_DIR, filename)
    
    try:
        logging.info(f"开始下载备份文件: {filename}")
        client = WebDavClient(
            cfg["webdav_url"], 
            auth=(cfg.get("webdav_user", ""), cfg.get("webdav_password", ""))
        )
        remote_path = f"{cfg.get('webdav_path', '/')}/{filename}".replace("//", "/")
        
        # 【修正】webdav4 使用 download_file 方法
        client.download_file(remote_path, local_path)
        
        process_restore_file(local_path)
    except Exception as e:
        logging.error(f"下载/还原过程出错: {e}")
        send_telegram_notify(f"下载/还原过程出错: {e}", success=False)

# --- 调度器设置 ---

scheduler = BackgroundScheduler(timezone=TZ_CN)

def schedule_backup_job(config: dict):
    """根据配置更新调度任务"""
    if scheduler.get_job('backup_job'):
        scheduler.remove_job('backup_job')
    
    hour = int(config.get('schedule_hour', 3))
    minute = int(config.get('schedule_minute', 0))
    
    scheduler.add_job(
        perform_backup, 
        CronTrigger(hour=hour, minute=minute, timezone=TZ_CN), 
        id='backup_job',
        replace_existing=True
    )
    logging.info(f"备份任务已调度: 每天 {hour:02d}:{minute:02d}")

scheduler.start()
initial_cfg = load_config()
schedule_backup_job(initial_cfg)


# --- API 路由定义 (除根路径外全部开启鉴权) ---

@app.get("/", response_class=HTMLResponse)
async def read_root():
    """返回前端页面 (不需要鉴权)"""
    index_path = "/app/static/index.html"
    if not os.path.exists(index_path):
        index_path = "app/static/index.html"
    
    if os.path.exists(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            return f.read()
    return "UI File Not Found. Please check deployment."

@app.get("/api/auth_check", dependencies=[Depends(check_auth)])
async def auth_check():
    """用于前端验证 Token 是否有效"""
    return {"status": "authenticated"}

@app.get("/api/config", dependencies=[Depends(check_auth)])
async def get_config():
    return load_config()

@app.post("/api/config", dependencies=[Depends(check_auth)])
async def update_config(config: dict):
    save_config(config)
    return {"status": "success", "message": "Configuration saved."}

@app.post("/api/backup/now", dependencies=[Depends(check_auth)])
async def trigger_backup_manual(background_tasks: BackgroundTasks):
    background_tasks.add_task(perform_backup)
    return {"status": "started", "message": "Backup started in background."}

@app.get("/api/backups", dependencies=[Depends(check_auth)])
async def list_backups():
    cfg = load_config()
    if not cfg.get("webdav_url"):
        return JSONResponse(status_code=400, content={"error": "WebDAV not configured"})
    
    try:
        client = WebDavClient(
            cfg["webdav_url"], 
            auth=(cfg.get("webdav_user", ""), cfg.get("webdav_password", ""))
        )
        # 【修正】必须加上 detail=True 才能返回包含 name, size 等信息的字典列表
        files = client.ls(cfg.get('webdav_path', '/'), detail=True)
        
        backup_files = []
        for f in files:
            # 确保是文件且名字包含 vw_backup_
            if f.get('type') != 'directory' and "vw_backup_" in f.get('name', ''):
                size_mb = round(int(f.get('size', 0)) / 1024 / 1024, 2)
                backup_files.append({
                    "name": f['name'],
                    "size": f"{size_mb} MB",
                    "last_modified": f.get('last_modified', '')
                })
        
        return sorted(backup_files, key=lambda x: x['name'], reverse=True)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.post("/api/restore", dependencies=[Depends(check_auth)])
async def restore_from_cloud(file_name: str, background_tasks: BackgroundTasks):
    background_tasks.add_task(download_and_restore, file_name)
    return {"status": "started", "message": f"Restoring {file_name} in background..."}

@app.post("/api/upload_restore", dependencies=[Depends(check_auth)])
async def upload_and_restore(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    local_path = os.path.join(TEMP_DIR, file.filename)
    try:
        with open(local_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        
        background_tasks.add_task(process_restore_file, local_path)
        return {"status": "started", "message": "File uploaded. Restore starting in background..."}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

@app.get("/api/logs", dependencies=[Depends(check_auth)])
async def get_logs():
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                return {"logs": "".join(lines[-100:])}
        except:
            return {"logs": "Error reading logs."}
    return {"logs": "No logs yet."}
