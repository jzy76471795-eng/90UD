90USDFO 服务端、玩家登录器和 GM 后台的明文源码。

| 目录 | 内容 |
| --- | --- |
| [server_proto](server_proto) | Python 游戏服务、控制接口、数据库迁移和原生模块源码 |
| [server-pack-win10](server-pack-win10) | Windows 服务端辅助模块 |
| [gm_manager](gm_manager) | Go GM 管理服务、桌面管理器和运维辅助源码 |
| [player_launcher](player_launcher) | 玩家登录器的 WPF 界面、Go 启动程序及辅助桥接源码 |
| [party_relay](party_relay) | Go 组队中继 |

## 构建与连接

- **服务端**使用 Windows x64 与 CPython 3.12.10。准备匹配的客户端、PVF、原生模块、SQLite 游戏库和运行依赖后，从仓库根目录运行 `python server_proto/start.py --help` 查看启动参数。PVF 路径可通过 `USDOF_PVF_PATH` 指定。
- **GM 后台**使用 Go 1.26。在 `gm_manager` 目录运行 `.构建GM.ps1`。首次运行 `gmserver.exe -init -config go-manager.json` 生成本机配置和管理令牌，再通过 `gmserver.exe -config go-manager.json` 启动。桌面 GM 使用该实例的管理地址和令牌。
- **玩家登录器**使用 .NET 8 SDK 构建，目标为 .NET 6 Windows x86。先构建 `player_launcher/bridge/GameAssistBridge.csproj`，用 x86 MSVC 编译 `wpf/Helper` 里的两个原生辅助程序，再构建 `wpf/DNFLogin.csproj`。设置 `GameAssistBridgeOutput` 指向桥接输出目录，设置 `ClientProtectionSecretsSource` 指向自备的保护构建输入。启用玩家登录接口时，在 GM 配置中填写 `launcher_listen`、`launcher_public_host`、`launcher_certificate` 和 `launcher_key`，再将 HTTPS 地址与对应证书配置到登录器。
- **GM WinRM 运维**使用自备凭据文件。启用时设置 `USDOF_GM_WINRM_CREDENTIALS`、`USDOF_GM_WINRM_COMPUTER_NAME` 和 `USDOF_GM_DEPLOY_ROOT`。

客户端与 PVF、正式服数据库、证书私钥、管理令牌和编译产物均需单独准备。

