#!/usr/bin/env python3
"""批量 SSH 执行工具：rk.py [--put 本地 远端] <cmd>  —— 默认四台全部执行

凭据通过环境变量提供，不要把密码写进仓库：
  LED_HOSTS  逗号分隔的主机列表，默认 172.20.32.27,172.20.32.28,172.20.32.30,172.20.32.31
  LED_USER   SSH 用户名，默认 linaro
  LED_PASS   SSH 密码（必填）
"""
import os, sys, paramiko

HOSTS = os.environ.get(
    "LED_HOSTS", "172.20.32.27,172.20.32.28,172.20.32.30,172.20.32.31").split(",")
USER = os.environ.get("LED_USER", "linaro")
PWD = os.environ.get("LED_PASS", "")
if not PWD:
    sys.exit("请先设置环境变量 LED_PASS（SSH 密码）")


def run(host, cmd, timeout=60):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=USER, password=PWD, timeout=10, banner_timeout=20)
    stdin, stdout, stderr = c.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", "ignore")
    err = stderr.read().decode("utf-8", "ignore")
    c.close()
    return out, err


def put(host, local, remote):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username=USER, password=PWD, timeout=10, banner_timeout=20)
    sftp = c.open_sftp()
    sftp.put(local, remote)
    sftp.close()
    c.close()


if __name__ == "__main__":
    args = sys.argv[1:]
    hosts = HOSTS
    putpair = None
    if args and args[0] == "--host":
        hosts = [args[1]]
        args = args[2:]
    if args and args[0] == "--put":
        putpair = (args[1], args[2])
        args = args[3:]
    if putpair:
        for h in hosts:
            put(h, putpair[0], putpair[1])
            print(h, "uploaded", putpair[1])
        sys.exit(0)
    cmd = " ".join(args)
    for h in hosts:
        print("=== %s ===" % h, flush=True)
        try:
            out, err = run(h, cmd)
            if out:
                print(out.rstrip())
            if err.strip():
                print("[stderr]", err.rstrip())
        except Exception as e:
            print("ERROR:", e)
