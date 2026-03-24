# 使用与 requirements.txt 中 playwright 版本严格对应的官方镜像
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

# 设置工作目录
WORKDIR /app

# 拷贝依赖清单并安装 Python 依赖
# (此时不需要再执行 playwright install，因为官方镜像已经自带了浏览器)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝项目源码
COPY . .

# 暴露 Flask 默认端口
EXPOSE 5000

# 运行整合了 Flask 和定时任务的脚本
# 加上 -u 参数强制 Python 不使用输出缓冲，这样在云平台的控制台能实时看到 print 的日志
CMD ["python", "-u", "main.py"]
