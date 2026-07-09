from fastapi import FastAPI
from pydantic import BaseModel
import subprocess
import uvicorn

app = FastAPI()

# 将网络字段设置为 Optional (即默认为 None)
class VMRequest(BaseModel):
    os: str
    name: str
    cpu: int
    memory: int
    disk_size: str
    ip: str | None = None
    gateway: str | None = None
    dns: str | None = None

@app.post("/api/v1/vm/create")
async def create_vm(req: VMRequest):
    # 1. 组装基础必填参数
    cmd = [
        "sudo", "python3", "/home/mzh/touch_kvm/kvm_vm_provision.py",
        "--os", req.os,
        "--name", req.name,
        "--cpu", str(req.cpu),
        "--memory", str(req.memory),
        "--disk-size", req.disk_size
    ]

    # 2. 动态追加网络参数（Dify 传了什么，这里就加什么）
    if req.ip:
        cmd.extend(["--ip", req.ip])
    if req.gateway:
        cmd.extend(["--gateway", req.gateway])
    if req.dns:
        cmd.extend(["--dns", req.dns])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return {"status": "success", "output": result.stdout}
    except subprocess.CalledProcessError as e:
        return {"status": "error", "error": e.stderr, "output": e.stdout}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
