# KVM 虚拟机自动化交付工具

基于 cloud-init (NoCloud) 的 KVM 虚拟机一键交付脚本，支持主流 Linux 发行版 Cloud Image 的自动下载、自定义资源配置与秒级创建。

## 目录

- [支持的发行版](#支持的发行版)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
- [使用方式](#使用方式)
  - [命令行模式](#命令行模式)
  - [交互模式](#交互模式)
  - [环境检测](#环境检测)
  - [查看发行版列表](#查看发行版列表)
- [参数说明](#参数说明)
- [工作原理](#工作原理)
- [凭据说明](#凭据说明)
- [常见问题](#常见问题)

## 支持的发行版

| Label | 发行版 | 镜像类型 | 默认用户 | 下载格式 |
|---|---|---|---|---|
| `ubuntu-24.04` | Ubuntu 24.04 LTS (Noble Numbat) | Cloud Image | `ubuntu` | `.img` |
| `ubuntu-22.04` | Ubuntu 22.04 LTS (Jammy Jellyfish) | Cloud Image | `ubuntu` | `.img` |
| `debian-13` | Debian 13 (Trixie) | Generic Cloud | `debian` | `.qcow2` |
| `debian-12` | Debian 12 (Bookworm) | Generic Cloud | `debian` | `.qcow2` |
| `openeuler-24.03-sp4` | openEuler 24.03 LTS SP4 | 虚拟机镜像 | `root` | `.qcow2.xz` |
| `openeuler-22.03-sp4` | openEuler 22.03 LTS SP4 | 虚拟机镜像 | `root` | `.qcow2.xz` |

> **说明**: 以上默认用户为 Cloud Image 出厂设置。通过本脚本创建的 VM 会使用你指定的自定义用户名覆盖出厂默认值。

## 环境要求

### 宿主机

- Linux 内核支持 KVM（`/dev/kvm` 存在）
- libvirtd 服务运行中
- Python 3.8+

### 依赖工具

| 工具 | 用途 | 安装命令 |
|---|---|---|
| `virt-install` | 创建虚拟机 | `apt install virtinst` |
| `qemu-img` | 磁盘镜像管理 | `apt install qemu-utils` |
| `virsh` | VM 管理 | `apt install libvirt-clients` |
| `genisoimage` | 生成 cloud-init ISO | `apt install genisoimage` |
| `brctl` | 网桥管理 | `apt install bridge-utils` |
| `wget` / `curl` | 下载镜像 | `apt install wget` |
| `xz` | 解压 openEuler 镜像 | `apt install xz-utils` |

### Python 库

| 库 | 用途 | 安装命令 |
|---|---|---|
| `PyYAML` | YAML 序列化 (user-data) | `pip3 install PyYAML` |
| `requests` | HTTP 下载校验 (可选) | `pip3 install requests` |

> PyYAML 和 requests 为可选依赖：缺失时脚本会降级使用 json 回退和 curl 回退。

### 一键安装所有依赖

```bash
# Debian/Ubuntu
sudo apt install -y virtinst qemu-utils libvirt-clients genisoimage bridge-utils wget xz-utils python3-pip
sudo pip3 install PyYAML requests

# 启动 libvirtd
sudo systemctl enable --now libvirtd
```

## 快速开始

```bash
# 1. 检测环境
sudo python3 kvm_vm_provision.py --check-env

# 2. 浏览可用发行版
sudo python3 kvm_vm_provision.py --list-os

# 3. 创建一台 Ubuntu 24.04 VM（DHCP 模式）
sudo python3 kvm_vm_provision.py \
    --os ubuntu-24.04 \
    --name demo-ubuntu \
    --cpu 4 \
    --memory 4096 \
    --disk-size 30G \
    --bridge virbr0 \
    --username devops \
    --password MyStr0ngP@ss

# 4. 创建一台 openEuler 24.03 SP4 VM（静态 IP）
sudo python3 kvm_vm_provision.py \
    --os openeuler-24.03-sp4 \
    --name demo-openeuler \
    --cpu 8 \
    --memory 8192 \
    --disk-size 50G \
    --bridge br0 \
    --ip 10.0.0.100/24 \
    --gateway 10.0.0.1 \
    --dns 223.5.5.5 \
    --username admin \
    --password MyStr0ngP@ss

# 5. 连接到 VM
virsh console demo-ubuntu
ssh devops@<vm-ip>
```

## 使用方式

### 命令行模式

所有参数通过 CLI 传入，适合脚本化和批量部署：

```bash
sudo python3 kvm_vm_provision.py \
    --os <发行版> \
    --name <VM名称> \
    --cpu <vCPU数> \
    --memory <内存MB> \
    --disk-size <磁盘容量> \
    --bridge <网桥> \
    [--ip <静态IP/前缀>] \
    [--gateway <网关>] \
    [--dns <DNS服务器>] \
    --username <用户名> \
    --password <密码>
```

### 交互模式

无参数交互式问答，适合手动快速创建：

```bash
sudo python3 kvm_vm_provision.py --interactive

# 或简写
sudo python3 kvm_vm_provision.py -i
```

交互流程：

```
选择发行版 (ubuntu-24.04/ubuntu-22.04/debian-13/debian-12/...): ubuntu-24.04
虚拟机名称: my-test-vm
CPU 核心数 [2]: 4
内存大小 (MB, 如 2048) [2048]: 4096
磁盘容量 (如 10G, 512M) [20G]: 30G
网桥名称 [virbr0]: virbr0
是否配置静态 IP？ [y/N]: n
初始用户名 [devops]: devops
初始密码 (≥6位): ********
```

### 环境检测

输出宿主机虚拟化环境完整报告：

```bash
sudo python3 kvm_vm_provision.py --check-env
```

输出包括：
- KVM 模块与 /dev/kvm 状态
- 关键工具 (virt-install / qemu-img / virsh / genisoimage / xz 等) 版本检测
- 可用网桥列表
- 现有虚拟机列表
- 镜像目录磁盘占用

### 查看发行版列表

```bash
python3 kvm_vm_provision.py --list-os
```

## 参数说明

### 核心参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `--os` | str | (必填) | 发行版 label，见[支持的发行版](#支持的发行版) |
| `--name` | str | (必填) | 虚拟机名称，同时用作 hostname |
| `--cpu` | int | 2 | vCPU 核心数 |
| `--memory` | int | 2048 | 内存大小 (MB)，最小 512 |
| `--disk-size` | str | 20G | 磁盘容量，支持 `10G` / `512M` / `1T` / `20`(GiB) |
| `--bridge` | str | virbr0 | 宿主机网桥名称 |

### 网络参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `--ip` | str | (无) | 静态 IPv4 地址/前缀，如 `192.168.122.100/24` |
| `--gateway` | str | (无) | 网关地址（指定 IP 时必填） |
| `--dns` | str | 223.5.5.5,119.29.29.29 | DNS 服务器，多个以逗号分隔 |

> 若未指定 `--ip`，VM 将使用 DHCP 获取地址。可通过 `virsh domifaddr <name> --source agent` 查询。

### 凭据参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `--username` | str | devops | 初始用户名（拥有 sudo 权限） |
| `--password` | str | (交互输入) | 初始密码（≥6 位）。命令行可传入但建议交互输入 |
| `--ssh-authorized-keys` | str | (无) | SSH authorized_keys 文件路径，注入到 VM |

### 高级参数

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `--image-dir` | str | `/var/lib/libvirt/images/templates` | 基础镜像缓存目录 |
| `--vm-dir` | str | `/var/lib/libvirt/images` | VM 磁盘存放目录 |
| `--force-download` | flag | false | 强制重新下载基础镜像 |
| `--no-autostart` | flag | false | VM 不随宿主机自动启动 |
| `--vnc-port` | int | -1(自动) | 指定 VNC 端口 |
| `--timezone` | str | Asia/Shanghai | 时区设置 |
| `--extra-cmd` | str | (无) | 额外的 cloud-init runcmd 命令（可多次指定） |

### 信息类参数

| 参数 | 说明 |
|---|---|
| `--list-os` | 列出所有支持的发行版及下载地址 |
| `--check-env` | 检测宿主机 KVM 虚拟化环境 |
| `-i`, `--interactive` | 交互式模式 |
| `-h`, `--help` | 显示帮助信息 |

## 工作原理

### 整体流程

```
┌─────────────────────────────────────────────────────┐
│ Phase 1: 环境检测                                    │
│   check_kvm()     → /dev/kvm + kvm 模块 + libvirtd  │
│   check_bridge()  → 网桥是否存在                      │
│   check_disk_space() → 磁盘剩余空间                   │
├─────────────────────────────────────────────────────┤
│ Phase 2: 基础镜像准备                                │
│   find_image_in_catalog() → 按 label 查找发行版      │
│   download_image() → wget/curl 拉取 + xz 解压        │
│   _verify_file_checksum() → SHA256/SHA512 校验       │
├─────────────────────────────────────────────────────┤
│ Phase 3: Cloud-Init 配置生成                         │
│   generate_user_data()    → 用户/密码/SSH/时区       │
│   generate_network_config() → 静态 IP/网关/DNS       │
│   write_cloud_init_iso()  → genisoimage 打包 ISO    │
├─────────────────────────────────────────────────────┤
│ Phase 4: 虚拟机创建                                  │
│   qemu-img create -b base.qcow2 → COW 写时复制       │
│   virt-install --import         → 秒级部署           │
└─────────────────────────────────────────────────────┘
```

### 磁盘策略：写时复制 (COW)

```bash
# 基础镜像仅下载一次（只读，可被所有 VM 共享）
qemu-img create -f qcow2 -b ubuntu-24.04-base.qcow2 -F qcow2 my-vm.qcow2 30G
```

- 基础镜像作为 backing file，**所有 VM 共享**，仅下载一次
- 每个 VM 仅存储增量差异数据，**磁盘创建秒级完成**
- VM 删除不影响基础镜像，新 VM 仍可基于同一镜像创建

### Cloud-Init 配置注入

通过 NoCloud 数据源将配置打包为 ISO 挂载为 CDROM：

```
cidata.iso
├── user-data       # 用户/密码/SSH/软件包/run commands
├── meta-data       # instance-id
└── network-config  # 静态 IP 配置 (cloud-init v2)
```

VM 首次启动时 cloud-init 会自动：
1. 读取 ISO 中的配置
2. 创建用户、设置密码
3. 配置静态 IP 或 DHCP
4. 扩容根分区到目标磁盘大小
5. 执行自定义 runcmd

### 目录结构

```
/var/lib/libvirt/
├── images/
│   ├── templates/              # 基础镜像缓存 (共享只读)
│   │   ├── ubuntu-24.04-base.qcow2
│   │   ├── debian-13-base.qcow2
│   │   └── openeuler-24.03-sp4-base.qcow2
│   └── <vm-name>.qcow2         # VM 增量磁盘 (COW)
├── cloud-init/
│   └── <vm-name>-cloud-init.iso  # cloud-init 配置 ISO
└── ...
```

## 凭据说明

### Cloud Image 默认凭据

各发行版 Cloud Image 的出厂默认用户（供参考，脚本会自动覆盖）：

| 发行版 | 默认用户 | 说明 |
|---|---|---|
| Ubuntu | `ubuntu` | sudo 组成员，密码锁定 |
| Debian | `debian` | sudo 组成员，密码锁定 |
| openEuler | `root` | 允许 root 登录 |

### 脚本行为

- 脚本会**创建你指定的新用户**并赋予 `sudo ALL=(ALL) NOPASSWD:ALL` 权限
- 同时设置 `root` 密码（与自定义用户密码相同）
- 启用 SSH 密码认证 (`ssh_pwauth: true`)
- 若提供了 `--ssh-authorized-keys`，同时配置 SSH 密钥登录

### 密码安全建议

- 命令行传入 `--password` 会在 shell history 中留下明文密码
- 生产环境建议使用交互模式输入密码
- 或创建完成后通过 `--ssh-authorized-keys` 注入公钥，禁用密码登录

## 常见问题

### Q: 创建失败："网桥 'br0' 不存在"

先确认宿主机上的网桥名称：

```bash
brctl show
# 或
ip link show type bridge
```

使用实际存在的网桥名称，如 `virbr0`。如需创建新网桥，参考：

```bash
sudo nmcli connection add type bridge ifname br0
```

### Q: openEuler 镜像下载后校验失败

openEuler 镜像为 `.qcow2.xz` 格式，脚本在解压前校验压缩包完整性。若出现校验错误，请使用 `--force-download` 重新下载：

```bash
sudo python3 kvm_vm_provision.py --os openeuler-24.03-sp4 --name test --force-download ...
```

### Q: VM 启动后无法获取 IP (DHCP 模式)

DHCP 模式下首次启动约需 30-60 秒完成 cloud-init 初始化。查看 IP：

```bash
# 等待 cloud-init 完成后
virsh domifaddr <vm-name> --source agent

# 若 agent 不可用，查看 ARP 表
arp -n | grep -i virbr
```

### Q: 如何查看 VM 的 cloud-init 初始化进度？

```bash
# 连接到 VM 控制台
virsh console <vm-name>

# 登录后查看 cloud-init 日志
sudo tail -f /var/log/cloud-init-output.log
sudo cloud-init status --wait
```

### Q: 如何删除 VM？

```bash
virsh destroy <vm-name>     # 强制关机
virsh undefine <vm-name>    # 删除定义
rm /var/lib/libvirt/images/<vm-name>.qcow2           # 删除磁盘
rm /var/lib/libvirt/cloud-init/<vm-name>-cloud-init.iso  # 删除配置
```

### Q: 基础镜像可以手动更新吗？

```bash
# 删除旧基础镜像
rm /var/lib/libvirt/images/templates/ubuntu-24.04-base.qcow2

# 重新下载最新版（使用 --force-download 或直接删除后重新创建 VM）
sudo python3 kvm_vm_provision.py --os ubuntu-24.04 --name updated-vm --force-download ...
```

> 注意：更新基础镜像后，基于旧镜像的已有 VM 不受影响（COW 隔离）。

## 许可证

MIT License
