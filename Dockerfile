# 阶段 1: 基础镜像
FROM vaultwarden/server:latest

# 设置非交互模式，防止安装 tzdata 时卡住
ARG DEBIAN_FRONTEND=noninteractive

# 1. 安装 Python 环境、构建工具以及 tzdata
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    libffi-dev \
    libssl-dev \
    supervisor \
    sqlite3 \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# 2. 设置时区为 Asia/Shanghai
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 设置工作目录
WORKDIR /

# 创建 Python 虚拟环境
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 复制 Python 依赖并安装
COPY app/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r /app/requirements.txt

# 复制应用程序代码
COPY app /app

# 复制 Supervisor 配置
COPY conf/supervisord.conf /etc/supervisord.conf

# 创建目录
RUN mkdir -p /conf /data

# 环境变量
ENV DATA_FOLDER=/data

# 暴露端口
EXPOSE 80 5000

# 启动命令
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisord.conf"]
