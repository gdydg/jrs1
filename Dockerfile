FROM python:3.10-slim

WORKDIR /app

# 1. 先拷贝依赖文件并安装 Python 库
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 2. 集中处理 Playwright 及其系统依赖，并在安装后立即清理缓存以极致压缩体积
RUN apt-get update && \
    playwright install-deps chromium && \
    playwright install chromium && \
    rm -rf /var/lib/apt/lists/*

# 3. 拷贝项目代码
COPY . .

# 暴露端口
EXPOSE 5000

# 运行程序
CMD ["python", "main.py"]
