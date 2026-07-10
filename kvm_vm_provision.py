#!/usr/bin/env python3
"""
KVM 虚拟机自动化交付脚本
========================

功能：
  1. 自动拉取 / 缓存主流 Linux 发行版 Cloud Image (QCOW2)
  2. 通过 cloud-init (NoCloud) 注入网络、用户、密码等配置
  3. 使用 virt-install --import 以写时复制 (COW) 方式秒级创建 VM

支持的发行版：
  - Ubuntu 24.04 LTS / 22.04 LTS (Noble / Jammy)
  - Debian 12 (Bookworm) Generic Cloud
  - openEuler 24.03 LTS / 22.03 LTS

依赖：
  - qemu-img, virt-install, virsh, genisoimage (或 xorriso)
  - Python 3.8+  +  PyYAML, requests (可选，用于在线校验)

用法：
  python3 kvm_vm_provision.py \
      --os ubuntu-24.04 \
      --name my-vm \
      --cpu 2 \
      --memory 2048 \
      --disk-size 20G \
      --bridge virbr0 \
      --ip 192.168.122.100/24 \
      --gateway 192.168.122.1 \
      --dns 223.5.5.5 \
      --username devops \
      --password MyStr0ngP@ss

  或者交互模式（无参数运行）：
  python3 kvm_vm_provision.py
"""

import argparse
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict, Tuple

# ---------------------------------------------------------------------------
# 第三方库 — 优雅降级 (lazy import)
# ---------------------------------------------------------------------------
try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

try:
    import requests as _requests
except ImportError:
    _requests = None  # type: ignore[assignment]


# ============================================================================
# 常量与配置
# ============================================================================

# ----- 默认目录 -----
DEFAULT_IMAGE_DIR = Path("/var/lib/libvirt/images/templates")
DEFAULT_VM_DIR = Path("/var/lib/libvirt/images")
DEFAULT_CLOUD_INIT_DIR = Path("/var/lib/libvirt/cloud-init")

# ----- Cloud Image 官方下载地址（截至 2026-07 已验证的最新稳定版） -----
# 格式说明：
#   label:       发行版简称 (参数 --os 使用)
#   description: 可读描述
#   url:         官方 QCOW2 下载地址
#   default_user:Cloud Image 出厂默认用户名（cloud-init 会覆盖）
#   checksum_url:SHA256SUMS 地址 (可选，用于完整性校验)

@dataclass
class OSImage:
    label: str
    description: str
    url: str
    default_user: str
    checksum_url: str = ""
    variant: str = ""


CLOUD_IMAGE_CATALOG: List[OSImage] = [
    OSImage(
        label="ubuntu-24.04",
        description="Ubuntu 24.04 LTS (Noble Numbat) Cloud Image",
        url="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
        default_user="ubuntu",
        checksum_url="https://cloud-images.ubuntu.com/noble/current/SHA256SUMS",
        variant="ubuntu24.04",
    ),
    OSImage(
        label="ubuntu-22.04",
        description="Ubuntu 22.04 LTS (Jammy Jellyfish) Cloud Image",
        url="https://cloud-images.ubuntu.com/jammy/current/jammy-server-cloudimg-amd64.img",
        default_user="ubuntu",
        checksum_url="https://cloud-images.ubuntu.com/jammy/current/SHA256SUMS",
        variant="ubuntu22.04",
    ),
    OSImage(
        label="debian-13",
        description="Debian 13 (Trixie) Generic Cloud Image",
        url="https://cloud.debian.org/images/cloud/trixie/latest/debian-13-genericcloud-amd64.qcow2",
        default_user="debian",
        checksum_url="https://cloud.debian.org/images/cloud/trixie/latest/SHA512SUMS",
        variant="debiansid",  # libvirt osinfo 中尚无 debian13，使用最近的
    ),
    OSImage(
        label="debian-12",
        description="Debian 12 (Bookworm) Generic Cloud Image",
        url="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-genericcloud-amd64.qcow2",
        default_user="debian",
        checksum_url="https://cloud.debian.org/images/cloud/bookworm/latest/SHA512SUMS",
        variant="debian10",  # libvirt osinfo 中 Debian 12 用 debian10
    ),
    OSImage(
        label="openeuler-24.03-sp4",
        description="openEuler 24.03 LTS SP4 虚拟机镜像",
        url="https://repo.openeuler.org/openEuler-24.03-LTS-SP4/virtual_machine_img/x86_64/openEuler-24.03-LTS-SP4-x86_64.qcow2.xz",
        default_user="root",
        checksum_url="https://repo.openeuler.org/openEuler-24.03-LTS-SP4/virtual_machine_img/x86_64/openEuler-24.03-LTS-SP4-x86_64.qcow2.xz.sha256sum",
        variant="openeuler24.03",
    ),
    OSImage(
        label="openeuler-22.03-sp4",
        description="openEuler 22.03 LTS SP4 虚拟机镜像",
        url="https://repo.openeuler.org/openEuler-22.03-LTS-SP4/virtual_machine_img/x86_64/openEuler-22.03-LTS-SP4-x86_64.qcow2.xz",
        default_user="root",
        checksum_url="https://repo.openeuler.org/openEuler-22.03-LTS-SP4/virtual_machine_img/x86_64/openEuler-22.03-LTS-SP4-x86_64.qcow2.xz.sha256sum",
        variant="openeuler22.03",
    ),
]


# ============================================================================
# 辅助工具函数
# ============================================================================

def run_cmd(cmd: List[str], check: bool = False, timeout: int = 300) -> subprocess.CompletedProcess:
    """执行外部命令并返回 CompletedProcess。

    Args:
        cmd:     命令列表
        check:   若为 True 且返回码非 0，则抛出 CalledProcessError
        timeout: 超时秒数（对下载等长操作会自动放大）

    Returns:
        subprocess.CompletedProcess
    """
    try:
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if check and cp.returncode != 0:
            raise subprocess.CalledProcessError(cp.returncode, cmd, output=cp.stdout, stderr=cp.stderr)
        return cp
    except FileNotFoundError:
        print(f"[ERROR] 命令未找到: {cmd[0]}，请确认已安装相关软件包。")
        sys.exit(1)


def check_root_or_sudo() -> bool:
    """检查是否以 root 或 sudo 权限运行。"""
    return os.geteuid() == 0


def parse_disk_size(size_str: str) -> int:
    """将人类可读的磁盘大小字符串转换为 GiB 整数。

    Examples:
        "10G"  -> 10
        "1T"   -> 1024
        "512M" -> 1 (向上取整)
        20      -> 20 (纯数字视为 GiB)
    """
    size_str = size_str.strip().upper()
    if size_str.endswith("G"):
        return int(float(size_str[:-1]))
    elif size_str.endswith("T"):
        return int(float(size_str[:-1]) * 1024)
    elif size_str.endswith("M"):
        raw = float(size_str[:-1]) / 1024
        return max(1, int(raw + 0.5))  # 向上取整，至少 1 GiB
    else:
        return int(size_str)  # 默认 GiB


def pretty_size(gib: int) -> str:
    """将 GiB 整数转为可读字符串。"""
    if gib >= 1024:
        return f"{gib / 1024:.1f} TiB"
    return f"{gib} GiB"


def confirm(prompt: str, default: bool = True) -> bool:
    """交互式 yes/no 确认。"""
    suffix = " [Y/n]: " if default else " [y/N]: "
    resp = input(prompt + suffix).strip().lower()
    if not resp:
        return default
    return resp in ("y", "yes")


# ============================================================================
# 环境检测
# ============================================================================

def check_kvm() -> bool:
    """检测宿主机 KVM 虚拟化是否可用。

    检查项：
      1. /dev/kvm 设备存在
      2. kvm 内核模块已加载
      3. libvirtd 服务正在运行
    """
    errors: List[str] = []

    # 1. /dev/kvm
    if not os.path.exists("/dev/kvm"):
        errors.append("/dev/kvm 设备不存在 —— 请在 BIOS 中启用 VT-x/AMD-V 并加载 kvm 模块。")
    else:
        print(f"  [OK] /dev/kvm 设备存在")

    # 2. 内核模块
    kvm_modules = list(Path("/sys/module").glob("kvm*"))
    if not kvm_modules:
        errors.append("kvm 内核模块未加载 —— 执行 'modprobe kvm && modprobe kvm_intel|kvm_amd'。")
    else:
        print(f"  [OK] KVM 模块已加载: {', '.join(m.name for m in kvm_modules)}")

    # 3. libvirtd
    cp = run_cmd(["systemctl", "is-active", "libvirtd"])
    if cp.stdout.strip() != "active":
        errors.append("libvirtd 服务未运行 —— 执行 'systemctl start libvirtd'。")
    else:
        print(f"  [OK] libvirtd 服务运行中")

    if errors:
        print("\n[FAIL] KVM 环境检测失败：")
        for e in errors:
            print(f"  - {e}")
        return False
    return True


def check_bridge(bridge: str) -> bool:
    """检查宿主机网桥是否存在。

    Args:
        bridge: 网桥名称 (如 br0, virbr0)
    """
    # 方式 1: brctl show
    cp = run_cmd(["brctl", "show"], check=False)
    if bridge in cp.stdout:
        print(f"  [OK] 网桥 '{bridge}' 存在 (brctl)")
        return True

    # 方式 2: ip link show type bridge
    cp = run_cmd(["ip", "link", "show", "type", "bridge"], check=False)
    if bridge in cp.stdout:
        print(f"  [OK] 网桥 '{bridge}' 存在 (ip link)")
        return True

    # 方式 3: ip link show <bridge>
    cp = run_cmd(["ip", "link", "show", bridge], check=False)
    if cp.returncode == 0:
        print(f"  [OK] 网桥 '{bridge}' 存在 (ip link <name>)")
        return True

    print(f"\n[FAIL] 网桥 '{bridge}' 不存在。")
    print("  可用网桥列表：")
    for line in cp.stdout.splitlines():
        if ": " in line:
            print(f"    - {line.split(':')[1].split('@')[0].strip()}")
    return False


def check_disk_space(path: Path, required_gib: int) -> bool:
    """检查指定路径所在分区的剩余空间是否足够。

    Args:
        path:         目标路径（会沿路径向上查找挂载点）
        required_gib: 需要的空间 (GiB)
    """
    try:
        stat = os.statvfs(path if path.exists() else path.parent)
        free_gib = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        if free_gib < required_gib:
            print(f"  [WARN] 磁盘空间不足: 需要 ~{pretty_size(required_gib)}, 剩余 {pretty_size(int(free_gib))}")
            return False
        print(f"  [OK] 磁盘空间充足: 需要 {pretty_size(required_gib)}, 剩余 {pretty_size(int(free_gib))}")
        return True
    except Exception as exc:
        print(f"  [WARN] 无法检测磁盘空间: {exc}")
        return True  # 不确定时放行


def check_tool(tool: str, pkg_hint: str = "") -> bool:
    """检查外部工具是否可用。"""
    path = shutil.which(tool)
    if path:
        print(f"  [OK] {tool} 已安装 ({path})")
        return True
    hint = f" —— 请执行: apt install {pkg_hint}" if pkg_hint else ""
    print(f"  [FAIL] 未找到 {tool}{hint}")
    return False


# ============================================================================
# 镜像管理
# ============================================================================

def find_image_in_catalog(key: str) -> OSImage:
    """根据 label 查找对应的 OSImage 条目。

    Args:
        key: 发行版 label，如 'ubuntu-24.04'

    Returns:
        OSImage 对象

    Raises:
        ValueError: 未知发行版
    """
    for img in CLOUD_IMAGE_CATALOG:
        if img.label == key:
            return img
    # 模糊匹配
    for img in CLOUD_IMAGE_CATALOG:
        if key.lower() in img.label.lower():
            return img
    raise ValueError(
        f"未知的发行版 '{key}'。可选值: {', '.join(i.label for i in CLOUD_IMAGE_CATALOG)}"
    )


def print_catalog():
    """打印所有可用发行版信息。"""
    print("\n" + "=" * 72)
    print("可用的 Cloud Image 发行版")
    print("=" * 72)
    for img in CLOUD_IMAGE_CATALOG:
        print(f"""
  {img.label:20s} — {img.description}
    下载地址  : {img.url}
    默认用户  : {img.default_user}
    SHA256SUMS: {img.checksum_url or '(未提供)'}""")
    print("=" * 72 + "\n")


def download_image(img: OSImage, dest: Path, force: bool = False) -> Path:
    """下载 Cloud Image 到本地，必要时自动解压 .xz，解压前校验完整性。

    Args:
        img:   OSImage 对象
        dest:  目标文件完整路径（最终应有的 .qcow2 路径）
        force: 若为 True，即使本地存在也重新下载

    Returns:
        已下载 / 已解压的 .qcow2 文件 Path

    Raises:
        RuntimeError: 下载或解压失败
    """
    if dest.exists() and not force:
        print(f"  [INFO] 镜像已存在: {dest}")
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)

    # 判断是否需要 xz 解压
    is_xz = img.url.endswith(".xz")

    # 下载暂存路径
    raw_dest = dest.with_suffix(dest.suffix + ".part.xz") if is_xz else dest.with_suffix(dest.suffix + ".part")

    print(f"  [INFO] 开始下载: {img.url}")
    print(f"         保存至: {dest}" + (" (将自动解压 .xz)" if is_xz else ""))

    # 使用 wget（更稳定）或 curl 作为后备
    downloader = None
    for tool in ["wget", "curl"]:
        if shutil.which(tool):
            downloader = tool
            break
    if not downloader:
        raise RuntimeError("未找到 wget 或 curl，无法下载镜像。")

    try:
        if downloader == "wget":
            run_cmd(
                ["wget", "-O", str(raw_dest), "--show-progress", "--timeout=600", img.url],
                check=True,
                timeout=1800,
            )
        else:  # curl
            run_cmd(
                ["curl", "-L", "-o", str(raw_dest), "--progress-bar", "--connect-timeout", "600", img.url],
                check=True,
                timeout=1800,
            )

        # xz 文件在解压前校验 checksum（checksum 是对 .xz 文件计算的）
        if is_xz and img.checksum_url:
            _verify_file_checksum(raw_dest, img.checksum_url, Path(img.url).name)
            # 校验通过后解压
            print(f"  [INFO] 正在解压 .xz 镜像...")
            run_cmd(["xz", "-d", "-k", "-f", str(raw_dest)], check=True, timeout=300)
            # xz -d -k 生成去掉 .xz 后缀的文件
            decompressed = raw_dest.with_suffix("")  # .part.xz -> .part
            decompressed.rename(dest)
            raw_dest.unlink(missing_ok=True)  # 删除 .xz 残留
            print(f"  [OK] 解压完成: {dest}")
        else:
            raw_dest.rename(dest)

        print(f"  [OK] 下载完成: {dest} ({pretty_size(int(dest.stat().st_size / (1024**3)))})")
        return dest
    except Exception:
        # 清理残留
        for f in [raw_dest, raw_dest.with_suffix(""), dest]:
            if f.exists():
                f.unlink(missing_ok=True)
        raise


def _verify_file_checksum(file_path: Path, checksum_url: str, target_filename: str) -> bool:
    """校验单个文件的 SHA256/SHA512 摘要（内部辅助函数）。

    Args:
        file_path:       要校验的本地文件路径
        checksum_url:    校验和文件 URL
        target_filename: 在 checksum 文件中查找的文件名
    """
    algo = "sha512" if "sha512" in checksum_url.lower() else "sha256"
    print(f"  [INFO] 正在校验 {algo.upper()}...")

    try:
        if _requests:
            resp = _requests.get(checksum_url, timeout=60)
            resp.raise_for_status()
            sums_text = resp.text
        else:
            cp = run_cmd(["curl", "-sL", checksum_url], check=True, timeout=60)
            sums_text = cp.stdout
    except Exception as exc:
        print(f"  [WARN] 无法获取校验和文件: {exc}")
        return True

    expected_hash = None
    for line in sums_text.splitlines():
        if target_filename in line:
            parts = line.split()
            if parts:
                expected_hash = parts[0]
            break

    if not expected_hash:
        print(f"  [WARN] 未在校验和文件中找到 {target_filename}，跳过校验。")
        return True

    h = hashlib.new(algo)
    with open(file_path, "rb") as fh:
        while chunk := fh.read(8 * 1024 * 1024):
            h.update(chunk)
    actual_hash = h.hexdigest()

    if actual_hash == expected_hash:
        print(f"  [OK] {algo.upper()} 校验通过")
        return True
    else:
        print(f"  [FAIL] 校验和不匹配！")
        print(f"         期望: {expected_hash}")
        print(f"         实际: {actual_hash}")
        return False


def verify_checksum(img: OSImage, image_path: Path) -> bool:
    """校验镜像 SHA256/SHA512（非 xz 文件）。

    对于 .xz 压缩的镜像，校验已在 download_image() 解压前完成，
    此函数直接跳过。
    """
    if img.url.endswith(".xz"):
        print("  [INFO] .xz 镜像已在校验阶段验证，跳过重复校验。")
        return True

    if not img.checksum_url:
        print("  [INFO] 未提供校验和 URL，跳过完整性校验。")
        return True

    return _verify_file_checksum(image_path, img.checksum_url, Path(img.url).name)


def prepare_base_image(os_key: str, image_dir: Path, force_download: bool = False) -> Tuple[OSImage, Path]:
    """获取 / 下载基础 Cloud Image，返回 (OSImage, 本地路径)。"""
    img = find_image_in_catalog(os_key)
    image_filename = f"{img.label}-base.qcow2"
    image_path = image_dir / image_filename

    if not image_path.exists() or force_download:
        download_image(img, image_path, force=force_download)
        verify_checksum(img, image_path)

    return img, image_path


# ============================================================================
# Cloud-Init / NoCloud 配置生成
# ============================================================================

def generate_user_data(
    hostname: str,
    username: str,
    password: str,
    ssh_authorized_keys: Optional[List[str]] = None,
    timezone: str = "Asia/Shanghai",
    extra_packages: Optional[List[str]] = None,
    extra_cmds: Optional[List[str]] = None,
) -> str:
    """生成 cloud-init user-data YAML 内容。

    Args:
        hostname:            主机名
        username:            自定义用户名
        password:            密码（明文，cloud-init 会自动 hash）
        ssh_authorized_keys: SSH 公钥列表
        timezone:            时区
        extra_packages:      额外要安装的软件包
        extra_cmds:          额外的 runcmd 命令

    Returns:
        cloud-init user-data YAML 字符串
    """
    data: Dict = {
        "hostname": hostname,
        "manage_etc_hosts": True,
        "timezone": timezone,
        "users": [
            {
                "name": username,
                "sudo": "ALL=(ALL) NOPASSWD:ALL",
                "groups": "sudo, adm, wheel, docker",
                "shell": "/bin/bash",
                "lock_passwd": False,
                "passwd": password,  # cloud-init 会自动使用 mkpasswd 方式 hash
            }
        ],
        "ssh_pwauth": True,
        "disable_root": False,
        "chpasswd": {
            "expire": False,
            "list": f"root:{password}",
        },
        "package_update": True,
        "package_upgrade": False,  # 首次启动不过度消耗时间
        "growpart": {
            "mode": "auto",
            "devices": ["/"],
        },
        "runcmd": [
            f"echo '>>> {hostname} cloud-init provisioned at $(date)' >> /var/log/cloud-init-done.log",
        ],
        "final_message": f"KVM VM {hostname} provisioned by cloud-init — up after $UPTIME seconds",
    }

    if ssh_authorized_keys:
        data["users"][0]["ssh_authorized_keys"] = ssh_authorized_keys

    if extra_packages:
        data["packages"] = extra_packages

    if extra_cmds:
        data["runcmd"] = extra_cmds + data["runcmd"]

    header = "#cloud-config\n"
    if yaml:
        return header + yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    else:
        # 纯手动回退 — 仅覆盖关键字段
        return header + json.dumps(data, indent=2, ensure_ascii=False)


def generate_network_config(ip: str, gateway: str, dns: str) -> str:
    """生成 cloud-init network-config (NoCloud v2 格式)。

    Args:
        ip:      IPv4 地址 + 前缀，如 '192.168.122.100/24'
        gateway: 网关地址
        dns:     DNS 服务器，多个以逗号分隔

    Returns:
        YAML 字符串
    """
    net = {
        "version": 2,
        "ethernets": {
            "eth0": {
                "dhcp4": False,
                "addresses": [ip],
                "gateway4": gateway,
                "nameservers": {
                    "addresses": [addr.strip() for addr in dns.split(",")],
                },
            }
        },
    }
    if yaml:
        return yaml.dump(net, default_flow_style=False, allow_unicode=True, sort_keys=False)
    else:
        return json.dumps(net, indent=2)


def write_cloud_init_iso(
    cloud_init_dir: Path,
    vm_name: str,
    user_data: str,
    network_config: str = "",
    meta_data: Optional[str] = None,
) -> Path:
    """将 user-data / meta-data / network-config 打包为 NoCloud ISO。

    Args:
        cloud_init_dir: 存放临时文件的目录
        vm_name:        虚拟机名称（用于命名输出文件）
        user_data:      cloud-init user-data 内容
        network_config: cloud-init network-config 内容
        meta_data:      cloud-init meta-data 内容（默认仅含 instance-id）

    Returns:
        ISO 文件路径
    """
    work_dir = Path(tempfile.mkdtemp(prefix=f"ci-{vm_name}-"))
    try:
        # 写入 user-data
        (work_dir / "user-data").write_text(user_data, encoding="utf-8")

        # 写入 meta-data
        if meta_data is None:
            meta_data = f"instance-id: {vm_name}-{datetime.now().strftime('%Y%m%d%H%M%S')}\n"
        (work_dir / "meta-data").write_text(meta_data, encoding="utf-8")

        # 写入 network-config（仅 cloud-init v2 支持）
        if network_config:
            (work_dir / "network-config").write_text(network_config, encoding="utf-8")

        # 打包 ISO — 使用 -graft-points 确保 ISO 根目录直接是 user-data/meta-data 文件
        iso_path = cloud_init_dir / f"{vm_name}-cloud-init.iso"
        cloud_init_dir.mkdir(parents=True, exist_ok=True)

        geniso = shutil.which("genisoimage") or shutil.which("mkisofs")

        if geniso:
            # genisoimage / mkisofs 支持 graft-points 语法:
            #   target_name=source_path
            cmd = [
                geniso,
                "-output", str(iso_path),
                "-volid", "cidata",
                "-joliet", "-rock",
                "-graft-points",
                f"user-data={work_dir / 'user-data'}",
                f"meta-data={work_dir / 'meta-data'}",
            ]
            if network_config:
                cmd.append(f"network-config={work_dir / 'network-config'}")
            run_cmd(cmd, check=True)
        elif shutil.which("xorriso"):
            # xorriso 不支持 graft-points，用 -path-list 或直接打包目录
            run_cmd(
                [
                    "xorriso", "-as", "mkisofs",
                    "-output", str(iso_path),
                    "-volid", "cidata",
                    "-joliet", "-rock",
                    str(work_dir),
                ],
                check=True,
            )
        else:
            raise RuntimeError("未找到 genisoimage 或 xorriso —— 请安装: apt install genisoimage")

        print(f"  [OK] Cloud-Init ISO 已生成: {iso_path}")
        return iso_path
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ============================================================================
# 虚拟机创建
# ============================================================================

def create_vm(
    name: str,
    base_image: Path,
    vm_image_dir: Path,
    disk_size_gib: int,
    vcpus: int,
    memory_mb: int,
    bridge: str,
    cloud_init_iso: Path,
    os_variant: str = "ubuntu24.04",
    vnc_port: int = -1,
    extra_args: Optional[List[str]] = None,
) -> bool:
    """使用 virt-install --import 创建虚拟机。

    Args:
        name:          虚拟机名称
        base_image:    基础 QCOW2 镜像路径
        vm_image_dir:  虚拟机磁盘存放目录
        disk_size_gib: 目标磁盘大小 (GiB)
        vcpus:         CPU 核心数
        memory_mb:     内存 (MB)
        bridge:        宿主机网桥名称
        cloud_init_iso:cloud-init ISO 路径
        os_variant:    libvirt osinfo 标识
        vnc_port:      VNC 端口 (-1 为自动分配)
        extra_args:    额外的 virt-install 参数

    Returns:
        True 成功
    """
    vm_image_dir.mkdir(parents=True, exist_ok=True)
    vm_disk = vm_image_dir / f"{name}.qcow2"

    # Step 1: 基于基础镜像创建 COW 后端
    print(f"\n  [INFO] 创建 COW 磁盘: {vm_disk}")
    run_cmd(
        [
            "qemu-img", "create",
            "-f", "qcow2",
            "-b", str(base_image),
            "-F", "qcow2",
            str(vm_disk),
            f"{disk_size_gib}G",
        ],
        check=True,
    )

    # Step 2: 检测基础镜像默认用户，用于 virt-install --cloud-init 兼容模式
    # （本脚本主要用 NoCloud ISO，此处为兼容）

    # Step 3: 构建 virt-install 命令
    cmd = [
        "virt-install",
        "--name", name,
        "--memory", str(memory_mb),
        "--vcpus", str(vcpus),
        "--disk", f"path={vm_disk},format=qcow2,bus=virtio,cache=none",
        "--disk", f"path={cloud_init_iso},device=cdrom",
        "--network", f"bridge={bridge},model=virtio",
        "--graphics", f"vnc,port={vnc_port},listen=0.0.0.0",
        "--video", "virtio",
        "--os-variant", os_variant,
        "--import",              # 不安装，直接导入现有磁盘
        "--noautoconsole",       # 不自动打开控制台
        "--hvm",
    ]

    # 添加 virtio RNG 以获得更好熵
    cmd.extend(["--rng", "/dev/urandom"])

    # 默认自动启动（除非显式禁用）
    if extra_args and "--no-autostart" in extra_args:
        extra_args.remove("--no-autostart")
        # 不添加 --autostart
    else:
        cmd.append("--autostart")

    if extra_args:
        cmd.extend(extra_args)

    print(f"\n  [INFO] 执行 virt-install 命令:")
    print(f"         {' '.join(shlex.quote(str(a)) for a in cmd)}")

    cp = run_cmd(cmd, check=False, timeout=300)
    if cp.returncode != 0:
        print(f"\n[FAIL] virt-install 失败:")
        print(cp.stderr)
        # 清理失败的磁盘
        if vm_disk.exists():
            vm_disk.unlink(missing_ok=True)
        return False

    print(f"  [OK] 虚拟机 '{name}' 创建成功！")
    print(cp.stdout)
    return True


# ============================================================================
# 信息展示
# ============================================================================

def print_env_report():
    """打印宿主机虚拟化环境信息。"""
    print("\n" + "=" * 72)
    print("宿主机虚拟化环境报告")
    print("=" * 72)

    # KVM
    print("\n[KVM 状态]")
    check_kvm()

    # 关键工具
    print("\n[关键工具]")
    tools = [
        ("virt-install", "virtinst"),
        ("qemu-img", "qemu-utils"),
        ("virsh", "libvirt-clients"),
        ("genisoimage", "genisoimage"),
        ("brctl", "bridge-utils"),
        ("wget", "wget"),
        ("xz", "xz-utils"),
    ]
    for tool, pkg in tools:
        check_tool(tool, pkg)

    # 网桥
    print("\n[可用网桥]")
    cp = run_cmd(["brctl", "show"], check=False)
    for line in cp.stdout.splitlines():
        print(f"  {line}")

    # 已有 VM
    print("\n[现有虚拟机]")
    cp = run_cmd(["virsh", "list", "--all"], check=False)
    for line in cp.stdout.splitlines():
        print(f"  {line}")

    # 磁盘使用
    print("\n[镜像目录]")
    for d in [DEFAULT_IMAGE_DIR, DEFAULT_VM_DIR]:
        if d.exists():
            total = sum(f.stat().st_size for f in d.glob("**/*") if f.is_file())
            count = len(list(d.glob("**/*")))
            print(f"  {d}: {count} 文件, {pretty_size(int(total/(1024**3)))}")
        else:
            print(f"  {d}: 不存在")

    print("\n" + "=" * 72)


# ============================================================================
# 主流程
# ============================================================================

def run_interactive() -> Dict:
    """交互式采集用户输入。"""
    print("\n" + "=" * 72)
    print("KVM 虚拟机自动化交付 — 交互模式")
    print("=" * 72)

    # 显示发行版列表
    print_catalog()

    # OS
    labels = [img.label for img in CLOUD_IMAGE_CATALOG]
    while True:
        os_choice = input(f"选择发行版 ({'/'.join(labels)}): ").strip()
        try:
            find_image_in_catalog(os_choice)
            break
        except ValueError as exc:
            print(f"  {exc}")

    # VM 名称
    while True:
        vm_name = input("虚拟机名称: ").strip()
        if vm_name:
            break
        print("  名称不能为空。")

    # vCPUs
    while True:
        try:
            vcpus = int(input("CPU 核心数 [2]: ").strip() or "2")
            if vcpus > 0:
                break
        except ValueError:
            pass
        print("  请输入正整数。")

    # Memory
    while True:
        mem_input = input("内存大小 (MB, 如 2048) [2048]: ").strip() or "2048"
        try:
            memory_mb = int(mem_input)
            if memory_mb >= 512:
                break
        except ValueError:
            pass
        print("  请输入正整数 (≥512MB)。")

    # Disk
    while True:
        disk_input = input("磁盘容量 (如 10G, 512M) [20G]: ").strip() or "20G"
        try:
            disk_gib = parse_disk_size(disk_input)
            if disk_gib >= 1:
                break
        except (ValueError, TypeError):
            pass
        print("  请输入有效容量 (如 10G)。")

    # Bridge
    bridge = input("网桥名称 [virbr0]: ").strip() or "virbr0"

    # Network
    use_static = confirm("是否配置静态 IP？", default=False)
    ip = gateway = dns = ""
    if use_static:
        ip = input("  IP地址/前缀 (如 192.168.122.100/24): ").strip()
        gateway = input("  网关 (如 192.168.122.1): ").strip()
        dns = input("  DNS (多个用逗号分隔) [223.5.5.5]: ").strip() or "223.5.5.5"

    # Credentials
    username = input("初始用户名 [devops]: ").strip() or "devops"
    while True:
        password = input("初始密码 (≥6位): ").strip()
        if len(password) >= 6:
            break
        print("  密码至少 6 位。")

    return {
        "os": os_choice,
        "name": vm_name,
        "cpu": vcpus,
        "memory": memory_mb,
        "disk_size": f"{disk_gib}G",
        "bridge": bridge,
        "ip": ip,
        "gateway": gateway,
        "dns": dns,
        "username": username,
        "password": password,
    }


def main():
    parser = argparse.ArgumentParser(
        description="KVM 虚拟机自动化交付脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例:
              %(prog)s --os ubuntu-24.04 --name demo --cpu 4 --memory 4096 --disk-size 30G --bridge br0 --ip 10.0.0.100/24 --gateway 10.0.0.1 --dns 223.5.5.5 --username devops --password MyStr0ngP@ss
              %(prog)s --interactive
              %(prog)s --list-os
              %(prog)s --check-env
        """),
    )

    # 信息类参数
    parser.add_argument("--list-os", action="store_true", help="列出所有支持的发行版镜像信息并退出")
    parser.add_argument("--check-env", action="store_true", help="检测宿主机 KVM 环境并退出")
    parser.add_argument("--interactive", "-i", action="store_true", help="交互模式")

    # VM 配置参数
    parser.add_argument("--os", type=str, help="发行版 label (如 ubuntu-24.04)")
    parser.add_argument("--name", type=str, help="虚拟机名称")
    parser.add_argument("--cpu", type=int, default=2, help="CPU 核心数 (默认: 2)")
    parser.add_argument("--memory", type=int, default=2048, help="内存 (MB) (默认: 2048)")
    parser.add_argument("--disk-size", type=str, default="20G", help="磁盘容量 (默认: 20G)")

    # 网络参数
    parser.add_argument("--bridge", type=str, default="virbr0", help="宿主机网桥 (默认: virbr0)")
    parser.add_argument("--ip", type=str, help="静态 IPv4 地址/前缀，如 192.168.122.100/24")
    parser.add_argument("--gateway", type=str, help="网关地址")
    parser.add_argument("--dns", type=str, default="223.5.5.5,119.29.29.29", help="DNS (多个逗号分隔)")

    # 凭据参数
    parser.add_argument("--username", type=str, default="devops", help="初始用户名 (默认: devops)")
    parser.add_argument("--password", type=str, help="初始密码 (若无则交互输入)")

    # 高级参数
    parser.add_argument("--image-dir", type=str, default=str(DEFAULT_IMAGE_DIR), help="基础镜像存放目录")
    parser.add_argument("--vm-dir", type=str, default=str(DEFAULT_VM_DIR), help="虚拟机磁盘存放目录")
    parser.add_argument("--force-download", action="store_true", help="强制重新下载基础镜像")
    parser.add_argument("--no-autostart", action="store_true", help="不随宿主机自动启动")
    parser.add_argument("--vnc-port", type=int, default=-1, help="VNC 端口 (默认自动分配)")
    parser.add_argument("--timezone", type=str, default="Asia/Shanghai", help="时区 (默认: Asia/Shanghai)")
    parser.add_argument("--ssh-authorized-keys", type=str, help="SSH authorized_keys 文件路径")
    parser.add_argument("--extra-cmd", type=str, action="append", help="额外的 cloud-init runcmd 命令 (可多次指定)")

    args = parser.parse_args()

    # ----- 模式：仅列出发行版 -----
    if args.list_os:
        print_catalog()
        return

    # ----- 模式：仅检测环境 -----
    if args.check_env:
        print_env_report()
        return

    # ----- 模式：交互式 -----
    if args.interactive:
        interactive_config = run_interactive()
        # 合并到 args
        for k, v in interactive_config.items():
            setattr(args, k.replace("-", "_"), v)

    # ----- 安全检查：需要 root -----
    if not check_root_or_sudo():
        print("[FAIL] 此脚本需要 root 或 sudo 权限运行（virt-install 要求）。")
        sys.exit(1)

    # ----- 合法性检查 -----
    if not args.os:
        print("[FAIL] 请指定 --os 参数。使用 --list-os 查看可用发行版。")
        sys.exit(1)
    if not args.name:
        print("[FAIL] 请指定 --name 参数。")
        sys.exit(1)
    if args.ip and not args.gateway:
        print("[FAIL] 指定 --ip 时必须同时指定 --gateway。")
        sys.exit(1)

    # 密码处理
    password = args.password
    if not password:
        import getpass
        while True:
            password = getpass.getpass("请输入初始密码 (≥6位): ")
            if len(password) >= 6:
                break
            print("  密码至少 6 位。")

    # ======================================================================
    # Phase 1: 环境检测
    # ======================================================================
    print("\n" + "=" * 72)
    print("Phase 1/4: 环境检测")
    print("=" * 72)

    image_dir = Path(args.image_dir)
    vm_dir = Path(args.vm_dir)
    disk_gib = parse_disk_size(args.disk_size)

    if not check_kvm():
        sys.exit(1)
    if not check_bridge(args.bridge):
        sys.exit(1)
    # 磁盘空间预估：基础镜像 ~1-2G + VM 磁盘 + 日志
    if not check_disk_space(image_dir, 5 + disk_gib):
        if not confirm("磁盘空间可能不足，是否继续？", default=False):
            sys.exit(1)

    # ======================================================================
    # Phase 2: 镜像准备
    # ======================================================================
    print("\n" + "=" * 72)
    print("Phase 2/4: 基础镜像准备")
    print("=" * 72)

    try:
        os_image, base_image_path = prepare_base_image(args.os, image_dir, args.force_download)
    except Exception as exc:
        print(f"[FAIL] 镜像准备失败: {exc}")
        sys.exit(1)

    print(f"\n  发行版    : {os_image.description}")
    print(f"  基础镜像  : {base_image_path}")
    print(f"  默认用户  : {os_image.default_user} (将被 cloud-init 覆盖)")

    # ======================================================================
    # Phase 3: Cloud-Init 配置
    # ======================================================================
    print("\n" + "=" * 72)
    print("Phase 3/4: Cloud-Init 配置生成")
    print("=" * 72)

    # SSH authorized_keys
    ssh_keys: Optional[List[str]] = None
    if args.ssh_authorized_keys:
        key_file = Path(args.ssh_authorized_keys)
        if key_file.exists():
            ssh_keys = [line.strip() for line in key_file.read_text().splitlines() if line.strip() and not line.startswith("#")]
            print(f"  [INFO] 已加载 {len(ssh_keys)} 个 SSH 公钥")
        else:
            print(f"  [WARN] authorized_keys 文件不存在: {key_file}")

    # 生成 user-data
    user_data = generate_user_data(
        hostname=args.name,
        username=args.username,
        password=password,
        ssh_authorized_keys=ssh_keys,
        timezone=args.timezone,
        extra_cmds=args.extra_cmd,
    )
    print(f"  [OK] user-data 已生成 ({len(user_data)} bytes)")

    # 生成 network-config（若有静态 IP）
    network_config = ""
    if args.ip:
        network_config = generate_network_config(args.ip, args.gateway, args.dns)
        print(f"  [OK] network-config 已生成 (静态 IP: {args.ip})")
    else:
        print(f"  [INFO] 未指定静态 IP，将使用 DHCP")

    # 打包 ISO
    cloud_init_dir = DEFAULT_CLOUD_INIT_DIR
    iso_path = write_cloud_init_iso(cloud_init_dir, args.name, user_data, network_config)

    # ======================================================================
    # Phase 4: 虚拟机创建
    # ======================================================================
    print("\n" + "=" * 72)
    print("Phase 4/4: 虚拟机创建")
    print("=" * 72)

    extra_virt_args = []
    if args.no_autostart:
        # 移除 --autostart，添加 --no-autostart
        # virt-install 中可通过在已有命令上调整；这里我们在 create_vm 内部处理
        extra_virt_args.append("--no-autostart")

    # 查找 os-variant
    os_variant = os_image.variant or "generic"

    success = create_vm(
        name=args.name,
        base_image=base_image_path,
        vm_image_dir=vm_dir,
        disk_size_gib=disk_gib,
        vcpus=args.cpu,
        memory_mb=args.memory,
        bridge=args.bridge,
        cloud_init_iso=iso_path,
        os_variant=os_variant,
        vnc_port=args.vnc_port,
        extra_args=extra_virt_args,
    )

    if not success:
        print("[FAIL] 虚拟机创建失败。")
        sys.exit(1)

    # ======================================================================
    # 完成
    # ======================================================================
    print("\n" + "=" * 72)
    print("虚拟机创建完成！")
    print("=" * 72)
    print(f"""
  名称       : {args.name}
  OS         : {os_image.description}
  vCPUs      : {args.cpu}
  内存       : {args.memory} MB
  磁盘       : {args.disk_size} (COW on {base_image_path.name})
  网桥       : {args.bridge}
  网络       : {'静态 ' + args.ip if args.ip else 'DHCP'}
  用户       : {args.username}
  VNC 端口   : {'自动' if args.vnc_port == -1 else args.vnc_port}

  连接方式:
    virsh console {args.name}
    virsh domdisplay {args.name}   # 查看 VNC 地址
    ssh {args.username}@{args.name if not args.ip else args.ip.split('/')[0]}

  Cloud-Init ISO 已挂载，首次启动时将自动配置。
  首次启动约需 30-60 秒完成 cloud-init 初始化。
""")

    # 尝试获取 IP（DHCP 场景）
    if not args.ip:
        print("  [INFO] DHCP 模式，首次启动后可通过以下命令查看 IP:")
        print(f"         virsh domifaddr {args.name} --source agent")

    print("=" * 72 + "\n")


if __name__ == "__main__":
    main()
