# 1. 使用官方提供的 Playwright Python 镜像作为基础 (内置了 Chromium 和所有系统环境)
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# 2. 设置工作目录
WORKDIR /app

# 3. 拷贝依赖清单并安装 Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. 拷贝项目所有源码
COPY . .

# 5. 提前创建 output 目录，避免读写权限报错
RUN mkdir -p output

# 6. 暴露 Flask 默认端口
EXPOSE 5000

# 7. 运行整合了 Flask 和后台线程的脚本
# 【关键细节】：加上 -u 参数强制 Python 不使用输出缓冲，这样你在 Render/Zeabur 的后台能实时看到 print 的日志！
CMD ["python", "-u", "main.py"]
