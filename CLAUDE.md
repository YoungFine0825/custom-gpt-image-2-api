# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

本仓库是一套 ComfyUI 自定义节点 (custom nodes),通过**用户自填的 OpenAI 兼容接口** (`base_url` + `api_key`) 调用 GPT-Image 系列模型做文生图与图生图。纯 Python,无编译步骤。

## 常用命令

依赖用 **ComfyUI 所使用的那个 Python 环境**安装(不要污染系统全局 Python):

```bash
pip install -r requirements.txt        # requests / Pillow / numpy
```

测试与检查(本仓库不用 pytest,无 lint 配置):

```bash
python -m py_compile __init__.py api_client.py nodes_gpt_image.py config_node.py

# 无网络单测：校验参数×模型联动、重试判定等纯逻辑。需 numpy/torch/requests/PIL 可导入。
# 它是自包含断言脚本(不是 pytest),整体运行、成功打印 ALL PASS；改校验逻辑后必跑。
python test_validation.py

# 前端扩展单测(需 node,无浏览器)：驱动**真实的** web/gpt_image_config_security.js
# 跑「新建 / 先填密钥后填地址 / 两节点同地址 / Clone / 粘贴 / 连续 clone / 旧存档
# 迁移 / 清空密钥」等操作序列。里面的假 litegraph 是照抄已安装前端产物的
# serialize / configure / clone 语义(见文件头注释),改前端逻辑后必跑。
node test_web_config.mjs
# 验证某用例真能抓 bug(拿旧实现跑,应当失败)：
# git show HEAD:web/gpt_image_config_security.js > /tmp/old.js
# GPTIMG_EXT=/tmp/old.js node test_web_config.mjs

# 联网连通性测试(不依赖 ComfyUI)：先验证目标网关能出图，再进 ComfyUI。
python test_api.py --base https://your-endpoint/v1 --key sk-... --model gpt-image-2 --prompt "..."
python test_api.py --base ... --key ... --prompt "..." --image a.png --image b.png   # 走 /images/edits
```

改动后需**重启 ComfyUI** 才生效;检查启动日志有无 `import failed`(通常是依赖没装进 ComfyUI 环境)。

## 架构大图

数据流:**配置节点 → `IMAGE_API_CONFIG` 元组 → 生成/编辑节点 → `api_client` → HTTP → IMAGE tensor**。

- `__init__.py` — ComfyUI 入口,导出 `NODE_CLASS_MAPPINGS` / `NODE_DISPLAY_NAME_MAPPINGS`。这里的内部 key 会写进工作流 `.json`,**改 key 会让旧工作流找不到节点**。
- `config_node.py` — `ImageAPIConfig`,输出自定义类型 `IMAGE_API_CONFIG`(就是 `(base_url, api_key)` 元组),一处配置可连多个节点。
- `web/gpt_image_config_security.js` — 前端扩展:把配置节点「密钥」widget 的 `widget.serialize` 设为 `false`,使 `api_key` **不写进保存/导出的工作流 JSON**(防分享泄露),但仍随 prompt 发给后端执行(执行走 `options.serialize`、持久化走 `widget.serialize`,是两条独立路径)。另把密钥存进浏览器 localStorage 并在载入/改地址时自动回填(**本机重开免重填**),存档**按配置节点各自归档**:键用节点内持久 uuid(存在 `node.properties.gptImageConfigId`,随工作流序列化)、而非 `base_url`——按 `base_url` 归档会让多个配置节点共享同一格、互相覆盖(v3.5.0 修复)。故 `__init__.py` 导出 `WEB_DIRECTORY`。
- `nodes_gpt_image.py` — 两个执行节点 `GPTImageGenerate`(`/images/generations`)与 `GPTImageEdit`(`/images/edits`)。它们是**薄封装**:只收集 widget 参数并转交 `api_client`,不含业务逻辑。端点由「用户选哪个节点」显式决定,**不靠有无参考图隐式切换**。
- `api_client.py` — **核心**:tensor↔bytes 转换、`build_params`(内含 `_validate` 单一校验入口)、`MODEL_RULES` 能力表、`generate_images`/`edit_images`、`_post_with_retry`、SSE 流式解析、带 TCP keepalive 的共享 `requests.Session`、`b64_json`/`url` 双通道结果读取。改行为基本都在这里。

## 必须遵守的设计不变量(读多文件才看得出的「为什么」)

1. **无预设网关 (no preset gateway)**:所有请求只发往用户配置的 `base_url`,代码中**不得**出现写死的域名、第三方图床、遥测/上报。这是仓库的核心安全承诺(见 `docs/usage-and-security.md`)。
2. **`default` 档 = 不发送该字段**:枚举参数(quality/background/output_format/moderation/input_fidelity 等)首项恒为 `"default"`,表示**不把该字段放进请求**,让服务端用默认值——避免给不支持该字段的兼容网关塞未知参数导致 400。新增枚举参数请沿用此约定。数值参数同理:`数量(n) = 0`(**且这是默认值**)表示不发送 `n`。

3. **两端点的参数编码不同,multipart 值必须走 `_form_value`**:`/images/generations` 走 JSON(`n` 是真整数),`/images/edits` 走 multipart —— 协议上**所有字段值只能是字符串**。故 edits 组表单时逐值经 `_form_value` 给出规范写法(int 不带小数点、bool 用小写 `true`/`false`),**不要退回 `str(v)`**(`str(True)`→`"True"`、`str(1.0)`→`"1.0"` 会被严格校验的网关判成非法而回 400)。这也是同一网关「文生图正常、图生图报参数错」的常见根源。

4. **前端 widget 顺序:`serialize:false` 的必须排在最后**:ComfyUI 前端两个方向不对称——`serialize()` 写 `widgets_values[i]` 用 widget **原始下标**(跳过 `serialize===false` 留空位),`configure()` 读取却用**压缩计数器**(只数 `serialize!==false` 的项)。二者仅在「所有 `serialize:false` 的 widget 都在最后」时一致。配置节点的「密钥」正是最后一个,**新增 widget 必须声明在「密钥」之前**,否则载入工作流时新 widget 会读到错位的值。
5. **参数×模型联动 = 执行期校验,不是 UI 联动**:ComfyUI 的 `INPUT_TYPES` 是静态的,widget 选项无法随另一参数实时变化(除非加前端 JS,本插件刻意不用)。因此模型相关约束都在 `api_client._validate` 里做。策略是 **`MODEL_RULES` 里的已知官方模型硬校验(违规 `raise ValueError`)、未知模型名(自定义网关)软放行(仅 `print` 警告)**——这是有意的兼容性设计,**不要对未知模型硬校验**。新增受支持模型只需在 `MODEL_RULES` 加一行。
6. **长耗时保活**:生图可能数分钟到十几分钟。防中间代理空闲切断靠三层:流式 SSE(`stream`+`partial_images`,官方机制)、TCP keepalive(`SO_KEEPALIVE`)、`(连接超时, 读取超时)` 元组分离。`_post_with_retry` 只重试「请求建立 + 首个状态码」阶段,**流式一旦开始迭代就不重试**,避免重放半消费的流。
7. **tensor / mask 约定**:ComfyUI `IMAGE` 是 `[B,H,W,C]` 的 0-1 float 批次;`MASK` 里 `1.0` 表示选中(要编辑)的区域,转 OpenAI mask 时 `alpha = (1 - mask) * 255`(透明处被编辑)。节点 `FUNCTION` 必须返回元组,单输出别漏逗号 `(img,)`。

## 相关文档

- **开发 ComfyUI 自定义节点**:先看仓库内 `docs/comfyui-custom-node-development.md`(已把官方规范调研落盘:注册、四个必需属性、`INPUT_TYPES` 结构、自定义类型、`WEB_DIRECTORY` 前端扩展等)。官方原文:<https://docs.comfy.org/custom-nodes/backend/server_overview> 与 <https://docs.comfy.org/custom-nodes/walkthrough>。若需节点内并发/异步(把执行函数写成 `async`),参考 ComfyUI async 节点支持 PR:<https://github.com/comfyanonymous/ComfyUI/pull/8830>。
- **部署/手动安装自定义节点**:官方手动安装文档 <https://docs.comfy.org/zh/installation/install_custom_node>(核心:克隆进 `custom_nodes/`、用 ComfyUI 自己的 Python 装 `requirements.txt`、重启并查 `import failed`)。
- **用法、两端点完整参数表、校验策略、安全数据流、版本迁移**:仓库内 `docs/usage-and-security.md`。
- 面向用户的功能说明与参数一览:`README.md`。

## 约定

- 界面标签、注释、文档、提交信息均用中文(重要术语可加英文/原文对照),与现有代码风格保持一致。
- 版本号维护在 `pyproject.toml` 的 `version`;发布到 Comfy Registry 依赖 `[tool.comfy]` 段。
