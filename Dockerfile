# 阶段 1: 基础镜像
FROM vaultwarden/server:latest

# 安装 Python 环境和工具
# Vaultwarden 基于 Debian (buster/bullseye) 或 Alpine。
# 官方 latest 标签目前通常是 Debian。
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    supervisor \
    sqlite3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /

# 复制 Python 依赖并安装
COPY app/requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt --break-system-packages

# 复制应用程序代码
COPY app /app

# 复制 Supervisor 配置
COPY conf/supervisord.conf /etc/supervisord.conf

# 创建必要的目录
RUN mkdir -p /conf /data

# 环境变量 (Vaultwarden 需要)
ENV DATA_FOLDER=/data

# 暴露端口: 80 (Vaultwarden), 5000 (Dashboard)
EXPOSE 80 5000

# 启动命令
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
