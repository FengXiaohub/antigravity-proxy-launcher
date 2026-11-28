# Antigravity 代理启动 / agy update

两个非官方 macOS 辅助工具：为 Antigravity 注入本机代理环境变量，以及安装它已经下载好的官方更新包。适合希望保持「规则模式 + 系统代理」、不开 TUN 的用户。

> Two local macOS helpers for Google Antigravity: launch the official executable with a local proxy environment, and install already-downloaded, signed updates. No background service or system proxy changes. The updater replaces the whole official bundle; it does not patch files inside it.

## 适用范围

本机对 Antigravity 2.35.x 的排查表明，登录和 Agent 通道使用的 Go 后台进程 `Contents/Resources/bin/language_server` 没有跟随 macOS 系统代理；从外部注入 `HTTP_PROXY` / `HTTPS_PROXY` 后可以连接。本项目基于这个已观察到的行为，不能保证未来版本仍采用相同路径和代理机制。

本项目不启用 TUN、不切全局模式、不新增虚拟网卡，也不创建第二份可运行的 Antigravity 或修改它的 bundle ID。TUN 的性能和路由行为取决于客户端及配置；Mihomo 支持多种协议栈和路由排除选项，不能概括为“全部使用 gVisor”或“都无法排除路由”。可参考 [Mihomo TUN 配置](https://wiki.metacubex.one/config/inbound/tun/)。本项目只是提供另一种启动方式。

## 安装：不需要输入命令

前置条件：

- Antigravity 已从官方渠道安装到 `/Applications/Antigravity.app`。
- Apple 命令行工具可用，`/usr/bin/python3` 可以运行，系统有 `osacompile`、`codesign`。未安装时，首次运行 Python 可能出现 Apple 的安装提示；安装完成后重试。
- 本机验证环境为 macOS 27.2.1（26B131）、Apple Silicon。其他 macOS 版本及 Intel Mac 尚未实机验证。

安装步骤：

1. 在 GitHub 点击 **Code → Download ZIP**，解压整个文件夹。
2. 在访达中双击 **Install.command**。它会打开终端窗口自动执行，结束后弹窗显示结果，用户无需敲命令；确认弹窗后可关闭窗口。
3. 两个图标安装到自己的 `~/Applications`：**Antigravity 代理启动.app** 和 **agy update.app**。已有同名助手会原位替换，不另外增加图标。

安装器先构建并验证两个助手，再替换原件；替换失败会尝试恢复原来的两个助手。中途断电或被终止后，重新双击同一安装文件，会先检查恢复记录。构建用的临时原件在成功或恢复完成后清理，不作为额外长期备份。

不需要 sudo。安装会写入上述助手目录、用户级安装状态目录，并向 Launch Services 注册两个助手图标。更新助手在本机构建后使用 ad-hoc 签名，**这不等于 Apple Developer ID 签名或公证**。首次打开被拦截或需要授权时，见下方说明；不要关闭整机安全策略。

### 首次双击 Install.command 被 Gatekeeper 拦截怎么办

从 GitHub 下载的 ZIP 会带有 quarantine 标记，而本项目没有 Apple 开发者签名与公证，所以首次双击 `Install.command` 时，macOS 可能提示「无法验证开发者」或「Apple 无法检查其是否包含恶意软件」。确认文件来自本仓库后，可以任选一种方式放行：

1. 在访达中**右键（或按住 Control 点按）`Install.command` → 打开**，在弹窗中再次点「打开」。
2. 若弹窗没有「打开」按钮（较新的 macOS 已取消右键放行），先关闭弹窗，到 **系统设置 → 隐私与安全性**，在页面下方找到关于 `Install.command` 的提示，点 **「仍要打开」** 并验证身份，再双击一次。

放行只针对这个文件。**不要执行 `sudo spctl --master-disable`** 或其他关闭 Gatekeeper 的操作，它会降低整台 Mac 的安全性，本工具也不需要。

### 「App 管理」权限与重装后重新授权

macOS 13 及以后的「App 管理」保护会限制一个应用移动或替换其他应用。「agy update」整包替换 `/Applications/Antigravity.app` 时，系统可能弹窗询问是否允许，或在 **系统设置 → 隐私与安全性 → App 管理** 中需要打开「agy update」的开关。若被拒绝，更新助手会在移动任何文件之前停止，提示检查该权限，原版本保持不变、不会留下未完成更新记录；授权后重新双击「agy update」即可。若 `/Applications/Antigravity.app` 属于其他账户，也会出现同类失败，需要由该账户或管理员处理。

更新助手是本机 ad-hoc 签名，每次重新运行 `Install.command` 都会生成新的签名身份，macOS 可能因此认为它是新程序：之前授予的「App 管理」等权限可能失效，需要在上述设置中重新打开（必要时先删除旧条目再添加）。这是 ad-hoc 签名的正常表现，不代表安装异常。

## 日常怎么用

| 场景 | 操作 |
|---|---|
| 日常打开 | VPN 连接后开启系统代理，双击「Antigravity 代理启动」 |
| 出现已下载的更新提示 | 等任务完成，正常退出 Antigravity，再双击「agy update」并在弹窗中确认 |
| **装了中文汉化补丁要更新** | **无需先还原汉化**，退出后直接双击「agy update」即可升级；升完恢复英文后，按需重打一次汉化补丁 |
| 更新完成 | 更新助手会请求通过原来的代理启动图标重新打开；若启动检查超时，可以稍后重试或选择恢复旧版 |
| 仅切换节点，端口没变 | 通常无需重启；已有连接可能短暂断开，是否自动重连取决于客户端与 Antigravity |
| 代理端口改变 | 按 Command-Q 完全退出，再双击代理启动器，让新进程读取新端口 |

**Antigravity 右上角的 Reboot and Update 不会转到本工具。** 需要手动双击「agy update」。不要同时让两个更新流程执行。

如果已有 Antigravity 在运行，启动器只激活现有窗口，无法给现有进程补上环境变量。对于无法确认由本启动器打开的进程，会弹窗说明；遇到登录或连接失败，请完全退出后通过代理启动器打开。

## 中文汉化客户端兼容说明

无论是否汉化，都可以配合本工具使用；不同汉化方式的表现如下：

| 汉化方式 | 日常「Antigravity 代理启动」 | 「agy update」更新 | 更新后需要重装汉化吗？ |
|---|---|---|---|
| **扩展商店官方中文语言包**（`Chinese (Simplified)`） | 直接支持 | 直接支持 | **不需要**（插件在用户目录，更新不丢失） |
| **一键静态汉化补丁**（修改 `app.asar` / 本地重签名） | 直接支持 | **直接支持（无需先还原英文）** | **需要重打一次补丁**（整包替换为官方新版后会回到英文） |
| **动态热注入汉化**（需专属启动器传 `--remote-debugging-port`） | 启动入口二选一（本启动器出于安全不默认开启调试端口） | 直接支持 | 取决于对应工具 |

补充说明：

- **为什么打过汉化补丁更推荐用 `agy update` 更新**：静态汉化补丁把应用改成了本地自签名后，Antigravity 右上角自带的 `Reboot and Update`（官方 `ShipIt`）往往会因原包签名不匹配而静默失败；退出客户端后直接双击 `agy update`，即可把已下载好的官方新包安全替换上去（新包仍强制校验 Google 官方签名与公证，旧版以 SHA-256 完整文件指纹备份，万一失败可连同原汉化状态原样回滚）。
- **旧版助手用户升级提示**：如果你之前装过较早版本的 `agy update.app` 并遇到过「Google 签名或应用完整性校验失败」，重新下载本仓库最新 ZIP 双击一次 `Install.command` 覆盖安装即可。

## 启动器做了什么

1. 读取 `scutil --proxy` 中启用的 HTTP、HTTPS、SOCKS 本机端点，只接受 `127.0.0.1`、`localhost`、`::1`。
2. 逐个用 `nc` 验证监听，再通过对应代理用 `curl` 请求 Google 的 `generate_204`，拿到 204 才接受。每次请求的连接超时为 5 秒、总等待上限为 12 秒，外层进程超时为 14 秒，避免较慢的可用节点被原来的 4 秒上限误判。检测本身会联网，未发送账号凭据。端口有响应但预检失败时，会提示检查超时或未返回预期结果；这不等于所有 Google 服务都不可用，也不代表账号或 Agent 通道已验收。
3. 系统配置不可用时，回退尝试 7890、7891、7893、7897、1080、12000，分别测试 HTTP 和 SOCKS。
4. 检查未完成更新记录和现有进程；随后设置大小写两组 `HTTP_PROXY`、`HTTPS_PROXY`、`ALL_PROXY` 及 `NO_PROXY`，最后 `execve` 官方二进制。

没有独立常驻服务；启动器进程被官方进程替代。环境变量只影响该进程及其子进程，不写入整机环境。Go `ProxyFromEnvironment` 使用 HTTP(S) 与 NO_PROXY；同时提供 ALL_PROXY 是给其他可能读取它的组件使用。Go 的代理匹配支持 NO_PROXY 的 IP CIDR，但这不代表 Chromium 的所有网络请求也遵循同样规则。

本机曾验证 `open --env` 没有传入预期变量，因此采用直接执行。若未来官方改变可执行文件路径、启动规则或系统代理接口，可能需要维护本工具。探测失败有提示，不能保证涵盖所有未来变化。

## 更新助手做了什么

- 优先寻找 `~/Library/Caches/com.google.antigravity.ShipIt/update.*/Antigravity.app`，选择版本号更新的候选；回退到 `com.google.antigravity/pending/Antigravity.zip`。
- ZIP 必须有 `update-info.json` 中的 SHA-512，实际比对后才解压；同时检查路径、链接、重复条目和解压体积上限。SHA-512 元数据本身不证明发布者身份。
- 候选更新包必须通过 `codesign --verify --deep --strict`，约束 Google Team ID **EQHXZ8M8AV** 及 `com.google.antigravity`。还要求有效的 Apple 公证凭证：先验证 stapled ticket，必要时在 Gatekeeper 评估已启用的情况下评估；不会修改安全策略或清除 quarantine。
- 当前已安装的旧版以路径、Bundle ID、版本号及完整文件指纹（SHA-256）校验备份，兼容打过静态汉化补丁（本地重签名）的客户端直接更新与原样回滚。
- 以确认票据、过期时间及文件指纹绑定用户刚刚确认的包；拒绝降级、跨卷替换、无法写入或发现官方 ShipIt 正在更新的情况。
- 正常请求退出并等待，不强制杀进程。只移动、替换整个官方包，不改包内文件，保留原有 ElectronAsarIntegrity。
- 操作前落盘恢复记录，替换失败尝试回滚；中断后下次双击检查恢复。主程序缺失或损坏且存在可信旧版时，弹窗确认后可恢复。
- 更新后请求代理启动器打开，以主进程及后台进程的监听状态做有限启动检查。**检查通过不等于已验证登录、Agent 实际任务或所有功能**。先前超时、后来手动启动正常时，会重新检查并继续后续更新。

**最多保留一份旧版备份。** 新一轮替换前先删除更老的备份；清理失败则停止，不继续产生第二份。备份以 `previous.bundle` 存放，不注册为第二个 Antigravity 图标。

不主动查询或下载线上更新。“未发现已下载包”和“本地包版本相同”均不表示已经是线上最新版。与官方 ShipIt 没有跨进程共享锁，只能反复检查，仍有很小的并发窗口；请按上述流程操作。

## 状态、隐私与卸载

| 位置 | 用途 |
|---|---|
| `~/Applications/Antigravity 代理启动.app`、`~/Applications/agy update.app` | 两个助手本身 |
| `~/Library/Application Support/AntigravityHelper` | 私有恢复记录、锁、至多一份旧版原件和启动进程标记 |
| `~/Library/Application Support/AntigravityHelperInstaller` | 安装记录及中断时尚待恢复的临时原件 |
| `~/Library/Caches/AntigravityHelperDownloads` | ZIP 解压暂存，可重新生成 |
| `~/Library/Logs/AntigravityHelper.log` | 本地结果日志，不记录 token、订阅链接或代理密码 |

旧版本位于 `~/Library/Caches/AntigravityHelper` 的状态会在安全取得锁后**移动**到持久目录，不复制备份；两处都有有效状态时会停止，请维护者确认，避免擅自合并或删除原件。

没有遥测上报，不改系统代理、DNS、登录项、LaunchAgent 或 `/etc`。安装会注册两个辅助工具自己的图标；官方 Antigravity 的 bundle ID 保持不变。日志和备份不会因为删除两个助手而自动消失。

卸载时先关闭更新弹窗、确保没有未完成的安装或更新，并确认官方 Antigravity 可以正常打开，再在访达删除两个助手。若希望清理残留，可通过访达「前往文件夹」打开上表路径，确认无需回滚后移除辅助工具的状态、缓存和日志。**不要删除 `~/.gemini/antigravity`，里面是会话数据。**

## 验证与限制

- 本次在 macOS 27.2.1、Antigravity 2.35.2 上验证了原位安装、Google 签名、公证和本机代理连通性；更新检查返回“本地包与当前版本相同”。
- 2.34.0 → 2.35.1 的真实升级有先前交接记录；它不是本次修改后重新执行的完整升级验收。
- 回归测试使用合成目录和模拟进程，覆盖替换中断、恢复失败、缺失/损坏应用、单份备份、安装失败及 AppleScript 状态交互。不会用真实会话或关闭正在运行的官方客户端。
- 本次修复后仍需在下一次真实更新、其他 Mac 首次安装时确认完整体验。测试不能保证未来 Antigravity、代理客户端或 macOS 更新均无需适配。
- 不解决 Google 账号限制、地区限制、账号二次验证、官方应用本身的卡死或内存问题。只支持无需认证的本机代理，不支持局域网代理端点。

维护者可以运行 `/usr/bin/python3 -B -m unittest discover -s tests -v`（与启动器、更新助手实际使用的系统 Python 一致），GitHub Actions 也配置了同一组测试与 AppleScript 编译检查。用户日常无需使用命令行。

## 源码结构

```
Install.command / install.sh / installer.py   构建、校验、原位安装与安装恢复
common/helper_state.py                        状态目录迁移与锁
launcher/                                     启动器入口、Python 逻辑与 plist
updater/                                      AppleScript 弹窗与更新后端
assets/                                       两个助手图标
tests/                                        隔离回归测试
docs/index.html                               GitHub Pages 主页
```

本项目非 Google 官方产品。源码采用 MIT，见 [LICENSE](LICENSE)。
