from fastapi import FastAPI, Response                  # 👈 1. 必须在这里导入 Response
from fastapi.exceptions import RequestValidationError  
from fastapi.responses import JSONResponse              
from pydantic import BaseModel
import subprocess
import uvicorn

app = FastAPI()

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    print(f"❌ 发现 422 参数校验错误!!! 详细原因: {exc.errors()}")
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()},
    )

class VMRequest(BaseModel):
    os: str
    name: str
    cpu: int
    memory: int
    disk_size: str
    ip: str | None = None
    gateway: str | None = None
    dns: str | None = None
    # 2. 增加默认凭据字段，如果 Dify 没传，就用默认的，防止脚本卡死
    username: str | None = "devops"
    password: str | None = "MyStr0ngP@ss"

@app.post("/api/v1/vm/create")
def create_vm(req: VMRequest):
    # 3. 组装命令，把 --username 和 --password 结结实实地安排上！
    cmd = [
        "sudo", "-n", "python3", "/home/mzh/touch_kvm/kvm_vm_provision.py",
        "--os", req.os,
        "--name", req.name,
        "--cpu", str(req.cpu),
        "--memory", str(req.memory),
        "--disk-size", req.disk_size,
        "--username", req.username,
        "--password", req.password   # 👈 核心：有了它，脚本就绝对不会去读 stdin 了！
    ]

    if req.ip: cmd.extend(["--ip", req.ip])
    if req.gateway: cmd.extend(["--gateway", req.gateway])
    if req.dns: cmd.extend(["--dns", req.dns])

    try:
        # 执行脚本并捕获完整的 stdout 和 stderr
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return Response(content=result.stdout, media_type="text/plain")

    except subprocess.CalledProcessError as e:
        error_log = f"❌ KVM脚本执行失败！\n[STDOUT]:\n{e.stdout}\n[STDERR]:\n{e.stderr}"
        return Response(content=error_log, media_type="text/plain")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
