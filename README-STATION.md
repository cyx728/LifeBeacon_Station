# LifeBeacon Station

独立开发版，启动 `Start-Station.cmd`。原接收器位于 `../GNU_Radio_LoRa`，冻结发行版位于 `../Releases/LifeBeacon-v1.0.0-20261027`。

支持 RTL-SDR 接收及 Black SDR HackRF Pro 半双工收发。选择 HackRF 后显示右侧发送栏；发送一次后恢复原先接收状态。取消会停止本次发送。HackRF RX LNA 为 0–40 dB、8 dB 步进；VGA 为 0–62 dB、2 dB 步进；AMP 默认关闭。TX VGA 为 0–47 dB，AMP 固定关闭。RTL 增益不作用于 HackRF。

## 短 CQ 格式

按本次约定：保留设备 ID、应用 CRC，消息类型为 0，类型后只有 CRC。共 10 字节：

| 字节 | 内容 |
|---|---|
| 0–1 | `4C 42` / LB |
| 2 | 版本 `01` |
| 3–6 | 设备 ID，uint32 小端 |
| 7 | 消息类型 `00` / CQ |
| 8–9 | CRC16-CCITT，初始值 FFFF，多项式 1021，覆盖字节 0–7，结果小端 |

例如设备 `0x59D8CF87`：`4C 42 01 87 CF D8 59 00 AA 74`。此短格式的设备 ID/类型顺序与原长信标格式不同，发射端必须采用相同定义。接收器同时保留原长格式兼容。短 CQ 无序号、生理和姓名字段，日志对应列留空；序号范围统计不包含无序号 CQ，行范围统计可包含它。

LoRa 使用显式头、物理层 CRC、8 个前导符号，编码率 1–4 表示 4/5–4/8。默认 BW125k、SF7、同步字 0x12，TX 采样率2M。每次只发送一帧，不循环。生成 IQ 幅度为 0.5，带前后零填充；状态 `hardware_stream_completed` 只表示驱动发送流程结束，不证明远端已收到。

接收日志与旧版一致，发送结果单独存入 `transmit-history.jsonl`，不混入接收统计。

## 本机运行环境

独立环境 `D:\LifeBeacon-build\station-runtime-verified`，libhackrf 2026.01.3；不升级原版运行环境。使用冻结版已测试的 GNU Radio/Qt 依赖，再替换新版 HackRF/USB/VC 运行库；pybind11-abi=4。重建脚本为 `../install_station_runtime.py`。

`verify_station.py` 验证短包 CRC、逐字节损坏拒绝、完整 LoRa IQ 编解码和 GUI。`probe_hackrf.py` 只接收、不发射。测试结果分别保存在 `station-test-result.json` 和 `hackrf-rx-test.json`。macOS 发行包是冻结接收版，不是此 Station 开发版。

本机已验证 HackRF Pro 实际2.4 MS/s采集240,000样本，RF/IF/BB增益回读为0/16/20 dB。本机未检测到 RTL-SDR，新版 RTL 接收路径通过原有模拟回归测试。

## 2026-10-28 收发切换修复

报文编码和样本转换在后台完成，准备期间继续接收。仅在真正发送时暂停接收并释放 USB 信号源；扫描解码分支、频谱和瀑布图控件保留，发送结束后重新接入信号源，不重建整套界面。发送按 IQ 时长主动停止，移除发送前等待流图 EOF 和额外200 ms休眠；取消请求由工作线程处理，不在界面线程排空 USB。恢复后的状态栏和发送日志记录实际接收暂停时间。

实际硬件测试：SF7/BW125k、2M TX采样率、VGA0/AMP关闭，10字节 CQ 的 IQ（含零填充）为81.216 ms；125k扫描下接收暂停约0.687 s，默认1MHz扫描下约1.969 s。暂停包括USB切换、缓冲排空及扫描调度器重启，随机器负载和SF变化。这是半双工设备，发送期间仍无法接收。

`verify_station_live.py --transmit-once --wide` 会实际发一帧低增益CQ，记录于 `station-tx-switch-test.json`。不加 `--transmit-once` 时只模拟发送故障；`--wide` 使用默认1MHz扫描。回归已确认流图/瀑布图控件未替换，恢复后瀑布图样本计数持续增长。实际远端正确接收尚未验证。
