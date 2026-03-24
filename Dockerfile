FROM python:3.10-slim

WORKDIR /app

# 安装必要的系统库以支持 Playwright
RUN apt-get update && apt-get install -y \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 Chromium 浏览器供 Playwright 抓取渲染使用
RUN playwright install chromium --with-deps

COPY . .

# 暴露端口
EXPOSE 5000

# 运行程序
CMD ["python", "main.py"]
