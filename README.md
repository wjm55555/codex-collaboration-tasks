# 协作任务

给 macOS 上的 Codex 用的局域网任务协作插件。两台 Mac 在同一个允许互访的 Wi‑Fi 上，双方都可以发任务和附件，也可以选对话、编辑提示词、接单和交付结果。

## 安装

从 [Releases](../../releases/latest) 下载 `collaboration-tasks-macos.zip`，解压后双击「安装协作任务.command」。从 Codex 左侧「… → 协作任务」打开，在「我发出的」添加设备。一方申请双向连接，对方同意一次后即可互发任务。

更多操作见[使用说明](使用说明.md)。

## 更新

协作任务打开时会检查本仓库的正式 Release。打开右上角「更新」可查看当前版本和上次检查结果，也可以点「检查更新」。开启「自动安装更新」后，有新版时会自动安装，接收端会等执行中的任务结束。安装前会校验发行签名。任务、附件和设备授权保存在本机，收发通过局域网完成。

## 发布新版本

1. 同步更新插件清单、面板和 `plugin/release.py` 中的版本号。
2. 通过本机保存的发行签名密钥构建 ZIP 和签名。

```sh
python3 scripts/build-release.py --signing-key /path/to/signing-key.pem
```

3. 创建与版本号对应的正式 Release，上传 `dist/collaboration-tasks-macos.zip` 和同名 `.sig`。发行公钥随客户端分发。
