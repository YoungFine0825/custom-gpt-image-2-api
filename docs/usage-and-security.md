# 用法、两端点完整参数、迁移与安全数据流

## 1. 节点总览(分类 `GPT-Image`)

| 节点(显示名) | 内部 key | 端点 | 作用 |
|----------------|----------|------|------|
| GPT-Image API 配置 (base_url + api_key) | `ImageAPIConfig` | — | 输出 `IMAGE_API_CONFIG`,即 `(base_url, api_key)` |
| GPT-Image 生成 (文生图) | `GPTImageGenerate` | `POST /images/generations` | 纯文本生图 |
| GPT-Image 编辑 (图生图) | `GPTImageEdit` | `POST /images/edits` | 1~8 张参考图 + 可选遮罩 |
| GPT-Image 尺寸规范化 (16倍数/边长) | `GPTImageSizeSnap` | — | 宽/高圆整到步长(默认16)倍数并 clamp 到 [最小边,最大边],输出 `宽`/`高` 两个 INT |

连线:`GPT-Image API 配置` 的「配置」输出 → 生成/编辑节点的「配置」输入。一个配置节点可以连多个节点。**端点由你选哪个节点决定**,不再靠有无参考图隐式判断。

**尺寸规范化节点用法**:把 `GPTImageSizeSnap` 的 `宽`/`高` 输出连到生成/编辑节点的「宽/高」输入(在生成/编辑节点上右键把「宽」「高」widget 转为 input 接口即可)。它只处理「步长倍数 + 边长范围」,把用户随手填的值吸附成合法尺寸;比例(1:3~3:1)与总像素范围仍由发请求前的 `_validate` 校验。核心逻辑见 `api_client.snap_dim`。

## 2. 请求如何构造(OpenAI Images API)

### 文生图 —— `POST {base_url}/images/generations`(JSON)

```json
{ "model": "gpt-image-2", "prompt": "...", "n": 1, "size": "auto" }
```

### 图生图 / 多参考图 —— `POST {base_url}/images/edits`(multipart/form-data)

- 文本字段:`model`、`prompt`、`n`、`size` 及下方启用的可选参数
- 参考图:重复的 `image[]` 文件字段(每张一个),本插件开放「图片1~图片8」
- 遮罩:连了「遮罩」时作为 `mask` 文件字段发送
- 不设置 `Content-Type`,由 HTTP 库自动生成 multipart 边界

## 3. 完整参数表

| 参数(节点标签) | 字段名 | 生成 | 编辑 | 取值 / 默认 |
|------------------|--------|:----:|:----:|-------------|
| 提示词 | `prompt` | ✅ | ✅ | 必填,≤32000 字符 |
| 模型 | `model` | ✅ | ✅ | 默认 `gpt-image-2`(可改) |
| 宽 / 高 | `size`(拼为 宽x高) | ✅ | ✅ | 两个 INT,**均为 0 = auto**;>0 时拼成 `宽x高` 发送。gpt-image-2 需 16 倍数、1:3~3:1、≤3840x2160;编辑端常见 `1024x1024/1536x1024/1024x1536` |
| 图片1 | `image[]` | — | ✅必填 | 第一张参考图 |
| 图片2~8 | `image[]` | — | 可选 | 追加参考图(最多 8) |
| 遮罩 | `mask` | — | 可选 | `MASK`;透明(选中)区域被编辑,alpha=(1-mask)*255 |
| 输入保真度 | `input_fidelity` | — | 可选 | `default`/`low`/`high`;**仅编辑端点**。`gpt-image-2` 忽略(恒为 high),`gpt-image-1.x` 生效 |
| 数量 | `n` | ✅ | ✅ | 0~10,**默认 0 = 不发送该字段**(由服务端定,通常 1;字段越少越不容易被挑剔的网关拒掉);要一次多张就填 2~10。遇网关错判 `n` 类型时保持 0(见本节末) |
| 质量 | `quality` | ✅ | ✅ | `default`/`auto`/`high`/`medium`/`low` |
| 背景 | `background` | ✅ | ✅ | `default`/`auto`/`transparent`/`opaque` |
| 输出格式 | `output_format` | ✅ | ✅ | `default`/`png`/`jpeg`/`webp` |
| 压缩质量 | `output_compression` | ✅ | ✅ | 0~100,**仅输出 jpeg/webp 时发送**,默认 100 |
| 审核级别 | `moderation` | ✅ | ✅ | `default`/`auto`/`low` |
| 流式 | `stream` | ✅ | ✅ | 布尔,默认关;长耗时开启保活(见第 5 节) |
| 流式预览数 | `partial_images` | ✅ | ✅ | 0~3,默认 2;流式时中途预览张数 |
| 超时秒数 | (读取超时) | ✅ | ✅ | 默认 900(15min),上限 3600;采用 (连接15s, 读取N秒) 元组 |
| 重试次数 | (客户端重试) | ✅ | ✅ | 0~5,默认 2;瞬时错误(429/5xx/超时/连接重置)自动重试(见第 5 节) |

> **`default` 档 = 不发送该字段**,让服务端用其默认值。这样面对不支持某字段的 OpenAI 兼容网关时不会因未知参数报 400。`output_compression` 还额外要求 `output_format` 为 jpeg/webp 才发送。`stream`/`partial_images` 仅在勾选「流式」时才发送。`input_fidelity` 仅编辑端点发送。`n` 亦沿用此约定:`数量=0` 时整字段不发。

### ⚠️ 两个端点的参数编码不同(常见 400 根源)

同一个参数在两个端点上「长得不一样」,这是 HTTP 协议决定的,不是插件的选择:

| | 文生图 `/images/generations` | 图生图 `/images/edits` |
|---|---|---|
| 编码 | `application/json` | `multipart/form-data` |
| `n=1` 实际发出 | `{"n": 1}` —— **真正的整数类型** | 表单字段 `n=1` —— **只能是字符串**,multipart 没有数字类型 |
| 服务端 | 直接拿到 int | 必须自己把 `"1"` 解析回 int |

因此**编辑端点更容易踩到网关的类型校验 bug**:若网关对 `n` 做整数强校验却没做字符串转换,就会回 `400 INVALID_PARAM: n 必须是 … 整数`,而同一网关的文生图完全正常。

插件侧已做到位(`api_client._form_value`):multipart 字段值统一给出「最无争议的写法」——int 输出纯十进制数字(不会是 `1.0`)、bool 输出小写 `true`/`false`(不会是 python 的 `True`);`n` 在 `build_params` 里先经 `_coerce_int` 收口成真 int 再编码。

**但 multipart 无法送出「真正的整数」——那是协议决定的。** 所以 v3.5.0 把「数量」的默认值改为 **0 = 整字段不发送**(与本插件枚举参数的 `default` 档同一套约定):不发 `n`,服务端就用自己的默认值(通常 1),这类网关 bug 自然绕开。

> ⚠️ **已存工作流不会自动跟进**:widget 默认值只作用于**新建节点**;旧工作流里存的还是原来的 `1`。若你正被此问题卡住,请把该节点的「数量」手动改成 0(或删掉重建节点)。

### 参数×模型联动校验(发请求前)

节点在发请求前按「模型」校验参数组合(单一入口 `api_client._validate`),尽早以明确中文提示拦截会被服务端拒绝的组合,省一次昂贵/缓慢的 API 往返。策略是**已知官方模型硬校验、未知网关软放行**:

| 模型 | size 约束 | transparent | input_fidelity |
|------|-----------|-------------|----------------|
| `gpt-image-2` | 16 倍数、比例 ≤3:1、最长边 ≤3840、总像素 655360~8294400 | ❌ 不支持(提示改 1.5) | ❌ 忽略(恒 high) |
| `gpt-image-1.5` / `gpt-image-1` / `gpt-image-1-mini` | `1024x1024`/`1536x1024`/`1024x1536`/`auto` | ✅ 需 png/webp | ✅ 生效 |
| 其它(自定义网关模型名) | 仅 16 倍数软提示,不拦截 | 软放行(仅 +jpeg 因无透明通道仍拦截) | 原样发送 |

> `transparent` + `jpeg` 是物理冲突(jpeg 无 alpha 通道),对任何模型都直接报错。校验只改 `input_fidelity`(遇不支持的模型丢弃)、不改其它字段。规则见 `api_client.py` 的 `MODEL_RULES`,新增模型只需在表里加一行。

### 结果读取

- **优先** `data[0].b64_json`(GPT image 模型默认返回 base64)
- **回退** `data[0].url`(DALL-E 风格;仅当服务端返回 url 时才去下载)
- `n>1` 且各图尺寸一致时合并为一个 `[N,H,W,3]` 批次输出;尺寸不一致则只输出第一张并打印提示。

## 4. 安全与数据流(重点)

- **无预设网关**:代码中不含任何写死的域名,一切请求发往你在配置节点填写的 `base_url`。
- **无第三方图床**:参考图直接以 `image[]` multipart 发往 `{base_url}/images/edits`,不经中转。
- **无遥测 / 上报**:没有 telemetry / analytics / beacon / sentry,也没有 `eval` / `exec` / `subprocess` / `socket` 等隐蔽执行或网络逃逸。
- **密钥只出现在请求头**:`Authorization: Bearer <key>`,绝不写进任何日志。上游报错时,插件会把**响应正文**提炼成摘要放进异常、并把完整正文打进 ComfyUI 控制台日志(见第 9 节);正文来自你自己的网关,不含密钥。
- **密钥不写进工作流**:前端扩展 (`web/gpt_image_config_security.js`) 把配置节点「密钥」widget 的 `serialize` 关闭,使 `api_key` 不进 `widgets_values`。

### ✅ 密钥泄露已修复(v3.4.0)

- **原问题**:配置节点的「密钥」是节点 widget,ComfyUI 保存/导出工作流时会把所有 widget 值写进 `.json` 的 `widgets_values`(导出 PNG 也内嵌同一份 workflow),导致**分享 / 上传 / 截图工作流会连同密钥一起泄露**。
- **关键约束**:ComfyUI 里「保存(Ctrl+S)」「导出」「输出图内嵌 workflow」三者都走同一个 `graph.serialize()`,前端**没有**可靠的「只在导出时剔除、保存时保留」钩子。故不去区分保存/导出,而是双管齐下(见下)。
- **修复原理**:ComfyUI 前端把「执行」与「持久化」分成两条独立路径——执行 (`graphToPrompt`) 读 widget 实时值发给后端、只在 `widget.options.serialize === false` 时跳过;持久化 (`LGraphNode.serialize`) 生成 `widgets_values`、在 `widget.serialize === false` 时跳过。
  1. 前端扩展对密钥 widget 设 `widget.serialize = false`(注意是 widget 自身属性,不是 `options.serialize`):密钥**照常参与执行**(后端仍拿得到 key),但**永不写进** `widgets_values`——任何序列化产物(本地保存的 `.json`、导出、PNG 内嵌)都不含它。
  2. 密钥单独存进浏览器 **localStorage**,**按配置节点各自归档**:载入工作流 / 改地址时自动回填,**本机重开无需重填**;但它不进工作流 JSON,分享给别人时对方拿不到。
- **索引安全**:密钥是配置节点最后一个 widget(`接口地址` 在前且照常保存),`configure()` 按 `serialize !== false` 过滤后定位,不会串位;旧的、已含 key 的工作流载入后其明文 key 会被丢弃、改由 localStorage 存档回填。
  > ⚠️ **给改动此节点的人**:ComfyUI 前端的两个方向**不对称**——`serialize()` 写 `widgets_values[i]` 用的是 widget 的**原始下标**(跳过 `serialize===false` 的项、留空位),而 `configure()` 读取时用的是**压缩计数器**(只数 `serialize!==false` 的项)。两者仅在「所有 `serialize:false` 的 widget 都排在最后」时才一致。密钥现在恰好是最后一个,所以安全;**若要新增 widget,必须加在「密钥」之前**(即 `INPUT_TYPES` 里声明在 `密钥` 前面),否则新 widget 会读到错位的值。

### ✅ 多个配置节点互相覆盖已修复(v3.5.0)

- **原问题**:v3.4.1 及以前,localStorage 存档**按 `base_url` 归档**(键 = 前缀 + `base_url`),那是**所有配置节点共享的一格**。于是:
  - 两个配置节点填同一个 `base_url`(例如同网关两个账号)时,**后改的密钥覆盖前一个的存档**;重开工作流两个节点都被回填成最后写入的那把密钥。
  - **Clone / 复制粘贴节点后必然踩中**:副本继承同一个 `base_url`,所以副本上配置密钥就是往原节点那一格写——刷新后**原节点的密钥被换成副本的**(观感就是"配置好的密钥丢了")。连续 clone 三份再各配一把密钥,刷新后四个节点会**全变成最后写入的那一把**。
  - **「先填密钥、后填地址」时密钥直接丢失**:存档键要用 `base_url` 算,地址还空着就算不出键,`saveKey` 静默 no-op;密钥又不进工作流,重开即没。
- **修复原理**:每个配置节点在 `node.properties.gptImageConfigId` 里持有一个 uuid(litegraph 的 `properties` 会随工作流序列化、并在 `configure()` 里逐键还原,是 ComfyUI 给节点挂持久化附加状态的既有机制;**不用 `node.id`**——它跨工作流会碰撞,见第 6.3 节)。存档键 = 前缀 + uuid,**写入只碰自己那一格**,节点之间再不互相覆盖。
- **回填优先级**:① 本节点自己的存档 → ② 同 `base_url` 下最近写入的存档(新建节点填个用过的地址即自动带出密钥,纯便利路径;**写入永远只写自己那格**,故不构成共享状态) → ③ v3.4.1 的旧版按-`base_url` 存档(**只读**,平滑迁移用,不再写入)。三者都**只在密钥 widget 为空时**才填,绝不覆盖手输值。用到 ②/③ 时会顺手落成本节点自己的存档,旧用户无感迁移。
- **Clone / 粘贴一定是全新节点**:复制节点时 litegraph 会连 `properties` 一起克隆,副本因此继承同一个 uuid。故**写入前做去重**:发现与其它活节点撞号就给本节点换新 uuid,并继承一份原存档的副本(所以副本开箱即可用、值看起来一样),此后两者**各写各的格子**,互不影响。
  > 两条路径的时序不同,都要照顾到:`LGraph.configure()`(载入工作流)与粘贴是**先把节点 add 进 graph、再逐个 `configure()`**;而 `LGraphNode.clone()`(菜单 Clone)是 **`createNode` → `configure()` → 由调用方 add**,即 `configure()` 时副本**还不在 graph 里**。去重同时挂在 `onConfigure` 与「写入前」两处,故两条路径都能覆盖。这些序列有 `test_web_config.mjs` 兜着(拿 v3.4.1 的实现跑会红 10 项)。
- **已知边界**:去重只看**当前打开的这张图**。把整个工作流「另存为」或导出后再导入成第二个工作流时,两份里的节点 uuid 相同、会共用同一格存档——它们本就源自同一份配置(同地址同账号),共用通常正是想要的;若要它们彼此独立,在其中一个上重新填一次密钥前先删掉该节点重建。
- **地址字段本就独立**:`接口地址` 是正常序列化的 widget,一直随工作流各存各的,不存在跨节点覆盖;但密钥被串改后节点的**实际生效配置**会跟着串,观感上就像「地址也被覆盖了」。


### ✅ 免连线模式:本地配置文件(v3.6.0 新增)

- **背景**:节点一多,每个生成/编辑节点都去连「配置」节点显繁琐。v3.6.0 起,生成/编辑节点在**未连「配置」输入**时,自动回退读取插件根目录的 `local_config.json`(`.gitignore` 已忽略、不进版本库):

  ```json
  { "base_url": "https://your-endpoint.example.com/v1", "api_key": "sk-..." }
  ```

- **优先级**:连了「配置」节点 → 完全用配置节点(显式连线优先);否则读本地文件;两者都没有才报错(错误信息会同时提示两条路)。
- **实时生效**:每次生成时实时读文件,改完即用,无需重启 ComfyUI。
- **安全**:凭据只在本机磁盘,不进工作流 `.json`、不进导出 PNG 内嵌 workflow、不经过浏览器 localStorage——比配置节点(密钥明文在 localStorage)暴露面更小。校验与配置节点共用 `api_client.validate_credentials`,两条路行为一致(空值 / 非 http(s) 地址都会在发请求前拦截)。
- **容错**:文件不存在 / JSON 损坏 / 缺字段 → 只打印警告并回退到「未提供凭据」报错,不会静默用错值,也不会让插件崩溃。

### ⚠️ 你仍需知道的残留风险

- **localStorage 是明文本机存储**:密钥明文存在浏览器 profile 里,**同源 JS 可读、也留在磁盘**。它严格优于「写进会被分享的工作流」,是社区处理前端密钥的标准做法,但**不是加密保险箱**;共用电脑/浏览器需注意。想更强隔离可改存 ComfyUI 服务端本地配置文件(需加后端路由)。清空「密钥」widget 会同步删除本机存档。
- **旧工作流可能已含明文密钥**:本次修复后新导出的工作流不再含密钥;但**修复前**已经保存/分享过的 `.json` 或 PNG 里可能仍有明文密钥。若你曾分享过,请**吊销并轮换 (revoke & rotate) 该密钥**。
- **换浏览器/清缓存需重填一次**:localStorage 不跨浏览器、不跨设备;换机器或清了站点数据后,首次需重新填一次密钥(填后即再次存档)。
- **base_url 决定数据去向**:密钥、提示词、参考图都会发给你填的地址。请只填你信任的服务端。
- 若服务端在响应里返回了 `url`,插件会去下载该 `url`(取回结果图片所必需);该地址由你的服务端控制。

## 5. 长耗时与保活

生图可能耗时数分钟到十几分钟。长时间无数据流动的连接容易被中间负载均衡/反向代理判空闲切断。三层应对:

1. **读取超时足够大**:节点「超时秒数」默认 900、上限 3600;`timeout=(15, N)`,连接 15s 快速失败、读取给足 N 秒。仅覆盖客户端自身。
2. **流式(官方保活机制,推荐)**:勾选「流式」→ 发送 `stream=true` + `partial_images`,服务端通过 SSE 分批推 `*.partial_image` 事件、最后 `*.completed` 带完整图。连接持续有数据流动,可越过中间代理的 idle timeout。插件解析:遍历 `data:` 行 JSON,按 `type` 取 `completed` 的 `b64_json`(取不到则用最后一个 partial 兜底)。**若响应 Content-Type 不是 `text/event-stream`(网关没按流式返回),自动回退普通 JSON 解析**,不会因开了流式而失败。
3. **TCP keepalive**:共享 `requests.Session` 上启用 `SO_KEEPALIVE`(+ 平台相关 `TCP_KEEPIDLE/INTVL/CNT`),维持 NAT/防火墙映射。对 L7 负载均衡的应用层 idle timeout 无效。
4. **瞬时错误重试(重试次数)**:遇 429/5xx/超时/连接重置时自动重试,**优先按服务端 `Retry-After` 头等待**,否则指数退避(`2^n` 秒,封顶 60s)。默认重试 2 次(总 3 次),0 关闭。**只重试「请求建立 + 首个状态码」阶段**——流式一旦开始迭代 SSE 就不再重试,避免半消费的流被重放;参考图以 bytes 传入,可安全跨重试重发。它兜的是"临时抖动",非法参数(400)不会重试。
   > 可重试码除 `429/500/502/503/504` 外,还含 **`520~527`**——那是 Cloudflare 边缘自有的一段 5xx(`520: Web server is returning an unknown error`、`524: A timeout occurred` 等)。很多 OpenAI 兼容网关挂在 Cloudflare 后面,这段码表示**边缘到源站之间**出错,与本次请求参数无关,属典型瞬时故障。生图耗时长时尤其常见,建议同时开「流式」保活。

> 端到端每层读超时都要 ≥ 生图时长。你能控本插件与自建网关;中间第三方 LB/nginx(`proxy_read_timeout` 等)不够大时,只有流式能保住连接。流式模式下多张(n>1)一般只回最终一张。

## 6. 中断、缓存与多工作流隔离

### 6.1 可中断(点 Cancel 能停)

ComfyUI 的中断是**协作式轮询 (cooperative polling)**:点 Cancel 只是把一个全局标志置真,节点必须**主动调用** `comfy.model_management.throw_exception_if_processing_interrupted()` 才会停下。单次阻塞的 `requests.post()` 会把线程挂在 OS 网络层、期间无任何 Python 代码执行,标志读不到——所以早期版本点了 Cancel 也要死等请求返回(对十几分钟的生图形同无法停止)。

现在的做法:把 HTTP 请求放进 **daemon 线程**,主线程**每 500ms 轮询一次**中断标志;流式模式则在每次 SSE 迭代间隙检查。中断时立即抛 `InterruptProcessingException`(它继承 `BaseException`,能穿透 `except Exception`,不会被重试逻辑误吞)。在 ComfyUI 外独立运行(`test_api.py` / 单测)时,`comfy` 模块不可用,自动降级为 no-op。

### 6.2 每次都真正调用 API(禁用输出缓存)

两个生图节点定义了 `IS_CHANGED` 恒返回 `float("nan")`。ComfyUI 默认按输入哈希缓存节点输出——相同 prompt/参数再次运行会**直接复用上次的图、根本不发请求**。但 API 生图是不确定的(同 prompt 每次结果不同),故本插件**禁用该缓存**,保证每次都真正向服务端请求、各自算各自的图。配置节点与尺寸规范化节点是纯确定性的,不加此项。

### 6.3 ⚠️ 多工作流「节点 id 碰撞」显示串台(ComfyUI 自身限制,非本插件可修)

**现象**:同一个 ComfyUI 服务上开两个工作流 A、B,生成/编辑节点的输出会互相覆盖——谁最后生成,两个工作流都显示谁的图。

**根因**(已核对 ComfyUI 源码):ComfyUI 全进程共享**唯一**的输出缓存 `caches.outputs`,以节点 `unique_id`(工作流 JSON 里的节点 id)为键、**不按工作流/标签分区**;前端也按 node id 认领预览图。而 litegraph 的节点 id 在单个图内从 1,2,3… 递增,两个各自新建(或"另存为"复制)的工作流**天然含相同 id**,于是在同一个缓存槽位上互相覆盖。对应上游 issue:[comfyanonymous/ComfyUI#6581](https://github.com/comfyanonymous/ComfyUI/issues/6581)。

**这不是本插件能在自身代码修掉的**——任何节点(含内置 `SaveImage`/`PreviewImage`)在 id 碰撞下都一样。`IS_CHANGED`(见 6.2)能保证两个工作流**确实各自发了请求**,但消除不了 ComfyUI 层面的显示串台。

**规避**:① 让两个工作流的节点 id 不重叠(**重建**其一,别用"另存为"复制;或前端手动改 id);② 一次只跑一个工作流,或把任务放进同一标签的队列串行跑;③ 最稳:开两个 ComfyUI 实例(不同 `--port`),缓存与队列各自独立。

**自查是否 id 碰撞**:导出两个工作流 `.json`,搜出问题节点的 `"id"` 字段,若相同即实锤;或只开一个工作流跑,现象消失即可佐证。

## 7. 迁移说明

### 从最初的 nerapi.com 版本
| 旧 | 现在 |
|----|------|
| 写死 `https://nerapi.com/v1` + 私有异步协议 `/api/generate` + 轮询 + 第三方图床 | 标准 OpenAI `/images/generations` 与 `/images/edits`,无预设网关 |
| Nano-Banana(nano-banana-pro/2)节点 | 已移除 |

### 从 2.0(单节点自动切换)到 3.0(拆两个节点)
- 旧的单个「GPT-Image-2」节点靠"有没有连参考图"自动切换端点;3.0 拆成 **GPT-Image 生成** 与 **GPT-Image 编辑** 两个节点,端点显式可选。
- 打开旧工作流后:按需替换为对应节点,重新连「配置」,并设置新增的 `quality/background/output_format/...` 等参数(留 `default` 即保持旧行为)。

## 8. 命令行自测

见 `test_api.py`(`--base` 与 `--key` 必填,无预设地址),支持 `--image`(可重复)、`--mask`、`--quality`、`--background`、`--output-format` 等,先验证服务端连通性再进 ComfyUI。

另有两个不联网的单测:

- `python test_validation.py` —— 参数×模型联动校验、重试判定、错误提炼等纯逻辑。
- `node test_web_config.mjs` —— 配置节点前端扩展的无浏览器单测:用照抄前端产物语义的假 litegraph,驱动**真实的** `web/gpt_image_config_security.js` 跑「新建 / 先填密钥后填地址 / 两节点同地址 / **Clone** / 粘贴 / 连续 clone / 旧存档迁移 / 清空密钥」等序列。拿 v3.4.1 的旧实现跑会红 10 项(`GPTIMG_EXT=/tmp/old.js node test_web_config.mjs`),即这些用例确实咬得住上面那类 bug。

## 9. 排查上游报错(错误信息怎么读)

**v3.5.0 前的问题**:错误信息是 `响应正文[:500]`。网关挂在 Cloudflare 后面时正文是一整页 HTML,前 500 字全是 `<!DOCTYPE>`/IE 条件注释/`<meta>`,**唯一有用的那句话(`<title>` 里的 `520: Web server is returning an unknown error`)恰好被切在外面**;JSON 错误也可能因外层包裹而被截断。结果是「看到报错却没法排查」。

**现在**每次非 200 都产出一条四段式信息(实现见 `api_client._http_error_message`):

```
[GPT-Image] edits 失败 (520 Origin Error): eaheng.com | 520: Web server is returning an unknown error | Error 520 …
  诊断: url=https://api.example.com/v1/images/edits; content-type=text/html; cf-ray=9f2c…-HKG; server=cloudflare
  本次发送: model='gpt-image-2', n='1', prompt='a cat…(共 239 字)', size='1024x1024'
  提示: 520~527 是 Cloudflare 边缘码,表示边缘到你的网关源站之间出错…
```

| 段 | 内容 | 怎么用 |
|---|------|--------|
| 首行 | 状态码 + reason + **从正文提炼的关键信息** | JSON 取 `error.message`(附 `code`/`type`);HTML 取 `<title>` + 去标签正文(剔除 `<script>`/`<style>`);其余按纯文本压空白 |
| 诊断 | URL(**已剥掉 query**)、`Content-Type`、`cf-ray`/`x-request-id`/`server`/`retry-after` 等 | 找网关方报障时把追踪 id 直接贴给对方 |
| 本次发送 | 本次实际发出的字段,**用 `repr` 打印** | 一眼区分 `n=1`(JSON 整数)与 `n='1'`(multipart 字符串),核对类型类 400 的根源(见第 3 节) |
| 提示 | 按状态码给的下一步 | 401/403 鉴权、404 端点/`/v1`、400 参数、413 体积、429 限流、5xx 与 520~527 上游 |

**完整正文不会丢**:摘要放进异常(避免整页 HTML 淹没 traceback),**完整响应正文打进 ComfyUI 控制台日志**(`[GPT-Image] … 上游完整响应正文 (N 字节)`)。异常框里看摘要,要看原始正文就翻控制台。

**重试日志也带原因**:`第 1/3 次返回 520,2.0s 后重试。原因: …`,不再只有一个裸状态码。

> 提示词长于 120 字会在「本次发送」里截断并标注总字数;密钥只在 `Authorization` 请求头里,**任何一段都不会出现密钥**。

