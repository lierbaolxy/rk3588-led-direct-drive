# -*- coding: utf-8 -*-
"""从 .30 拉取编译好的 nova_kmod.ko 到本地备份目录

凭据通过环境变量提供：LED_HOST / LED_USER / LED_PASS
"""
import os, sys, paramiko

HOST = os.environ.get("LED_HOST", "172.20.32.30")
USER = os.environ.get("LED_USER", "linaro")
PWD = os.environ.get("LED_PASS", "")
if not PWD:
    sys.exit("请先设置环境变量 LED_PASS（SSH 密码）")

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, password=PWD, timeout=10)
sftp = cli.open_sftp()
sftp.get("/userdata/nuowa/kmod/nova_kmod.ko",
         r"o:\发送卡\发送卡\kmod_backup_20261104\nova_kmod.ko")
sftp.close()
cli.close()
print("fetched")
