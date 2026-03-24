FROM python:3.10-slim

# 核心修复：强制 apt-get 采用非交互模式，遇到配置提示自动采用默认值，防止卡死
ENV DEBIAN_FRONTEND=noninteractive

WORKDIR /app

# 1. 先安装 Python 依赖包
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2. 更新系统 -> 安装浏览器底层依赖 -> 安装 Chromium -> 清理缓存
# 改用 python -m playwright 确保能准确调用到刚刚 pip 安装的模块
RUN apt-get update && \
    python -m playwright install-deps chromium && \
    python -m playwright install chromium && \
    rm -rf /var/lib/apt/lists/*

# 3. 拷贝项目代码
COPY . .

# 暴露端口
EXPOSE 5000

# 运行程序
CMD ["python", "main.py"]
