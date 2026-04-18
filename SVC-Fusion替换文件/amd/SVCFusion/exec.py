import json
import os
import socket
import subprocess
import sys
import threading
import time

from loguru import logger


def get_executable():
    # if sys.platform == "win32":
    #     return '"' + sys.executable + '"'
    # else:
    #     return "'" + sys.executable + "'"
    return sys.executable


executable = get_executable()


def exec_it(command):
    accumulated_output = ""
    try:
        # command = 'python -c "import time; [print(i) or time.sleep(1) for i in range(1, 6)]"'
        result = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=True,
            text=True,
        )
        accumulated_output += f"Command: {command}\n"
        yield accumulated_output
        progress_line = None
        for line in result.stdout:
            if r"it/s" in line or r"s/it" in line:  # 防止进度条刷屏
                progress_line = line
            else:
                accumulated_output += line
            if progress_line is None:
                yield accumulated_output
            else:
                yield accumulated_output + progress_line
        result.communicate()
    except subprocess.CalledProcessError as e:
        result = e.output
        accumulated_output += f"Error: {result}\n"
        yield accumulated_output


def exec(command: str):
    # 处理 executable 路径中的空格
    if command.startswith(executable) and " " in executable:
        command = f'"{executable}"' + command[len(executable) :]
    logger.info(f"Run command: {command}")
    code = subprocess.call(command, shell=True)
    logger.info(f"Command finished with code: {code}")
    return code


def start_with_cmd(cmd: str):
    """
    启动一个外部命令。

    当环境变量 SVCFUSION_LAUNCHER_IPC_PORT 存在时，通过 TCP Socket 连接到 launcher
    并发送执行命令请求；否则在 Windows 上使用 wt 启动，其他平台直接 os.system。
    """
    # cmd = "call .conda\\Scripts\\activate.bat" + " && " + cmd
    # 处理 executable 路径中的空格
    if cmd.startswith(executable) and " " in executable:
        cmd = f'"{executable}"' + cmd[len(executable) :]
    logger.info(f"Run command with cmd: {cmd}")

    ipc_port = os.environ.get("SVCFUSION_LAUNCHER_IPC_PORT")
    if ipc_port:
        try:
            port = int(ipc_port)
            _send_ipc_exec_request(cmd, port)
            return
        except ValueError:
            logger.warning(
                f"SVCFUSION_LAUNCHER_IPC_PORT 值无效: {ipc_port}，回退到本地执行"
            )
        except Exception as e:
            logger.error(f"IPC 通信失败: {e}，回退到本地执行")

    # 本地执行（不弹新窗口，后台运行，进度实时回显到主窗口）
    import subprocess as _sp
    CREATE_NO_WINDOW = 0x08000000
    full_cmd = f"chcp 65001 >nul & set PYTHONIOENCODING=utf-8 & set PYTHONLEGACYWINDOWSSTDIO=utf-8 & set PYTHONUNBUFFERED=1 & {cmd}"

    log_path = "train_output.log"
    if os.path.exists(log_path):
        open(log_path, "w", encoding="utf-8").close()

    log_file = open(log_path, "w", encoding="utf-8")
    proc = _sp.Popen(
        full_cmd,
        shell=True,
        stdout=log_file,
        stderr=_sp.STDOUT,
        creationflags=CREATE_NO_WINDOW,
    )
    logger.info(f"训练已在后台启动 (PID: {proc.pid})，输出写入 {log_path}")

    def _tail_log():
        pos = 0
        while True:
            if not os.path.exists(log_path):
                time.sleep(0.2)
                continue
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    f.seek(pos)
                    new_lines = []
                    for line in f:
                        stripped = line.strip()
                        if stripped and ("epoch" in stripped.lower() or "loss" in stripped.lower() or "step" in stripped.lower() or "training" in stripped.lower()):
                            print(stripped, flush=True)
                        elif stripped and ("error" in stripped.lower() or "traceback" in stripped.lower() or "warning" in stripped.lower()):
                            print(stripped, flush=True)
                    pos = f.tell()
            except Exception:
                pass
            ret = proc.poll()
            if ret is not None:
                time.sleep(0.5)
                try:
                    with open(log_path, "r", encoding="utf-8") as f:
                        f.seek(pos)
                        remaining = f.read().strip()
                        if remaining:
                            for line in remaining.split("\n"):
                                s = line.strip()
                                if s:
                                    print(s, flush=True)
                except Exception:
                    pass
                print(f"[训练进程已退出，退出码: {ret}]", flush=True)
                break
            time.sleep(0.3)

    t = threading.Thread(target=_tail_log, daemon=True)
    t.start()


def _send_ipc_exec_request(cmd: str, port: int, host: str = "127.0.0.1"):
    """
    通过 TCP Socket 向 launcher 发送执行命令请求。

    协议：JSON over TCP，以换行符分隔消息。
    需要通过 access_token 进行鉴权。
    """
    access_token = os.environ.get("SVCFUSION_LAUNCHER_ACCESS_TOKEN")
    if not access_token:
        raise ValueError("缺少环境变量 SVCFUSION_LAUNCHER_ACCESS_TOKEN，无法进行鉴权")

    request = {
        "type": "exec",
        "cmd": cmd,
        "access_token": access_token,
    }
    payload = json.dumps(request, ensure_ascii=False) + "\n"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(5.0)
        sock.connect((host, port))
        sock.sendall(payload.encode("utf-8"))
        # 可选：等待 ACK
        response_data = b""
        try:
            while True:
                chunk = sock.recv(1024)
                if not chunk:
                    break
                response_data += chunk
                if b"\n" in response_data:
                    break
        except socket.timeout:
            logger.warning("等待 launcher 响应超时")
        if response_data:
            try:
                response = json.loads(response_data.decode("utf-8").strip())
                if response.get("status") != "ok":
                    logger.warning(f"launcher 返回非 ok 状态: {response}")
            except json.JSONDecodeError:
                logger.warning(f"无法解析 launcher 响应: {response_data}")