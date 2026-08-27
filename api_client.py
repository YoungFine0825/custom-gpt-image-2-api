# -*- coding: utf-8 -*-
"""OpenAI 兼容 (OpenAI-compatible) 图像客户端。

设计原则 (design principles):
  - 所有请求只发往用户在「GPT-Image API 配置」节点里填写的 base_url。
  - 没有任何预设网关 (no preset gateway)、没有第三方图床 (no third-party image host)、
    没有遥测/数据上报 (no telemetry)。密钥、提示词、图片仅直发用户配置的地址。

两个端点各由一个独立 ComfyUI 节点驱动 (each endpoint = one node)：
  POST {base_url}/images/generations   (JSON)       -> 文生图 (text-to-image)
  POST {base_url}/images/edits         (multipart)   -> 图生图/多参考图 (image-to-image)

长耗时保活 (keep-alive for long jobs)：生图可能耗时数分钟到十几分钟。
  - 流式 (stream=true + partial_images)：服务端在生成过程中通过 SSE 分批推送
    partial image 事件，连接持续有数据流动，可避免中间负载均衡/代理的空闲超时
    (idle timeout) 切断连接。这是 OpenAI Images API 文档提供的官方机制。
  - TCP keepalive：另在 socket 层启用 SO_KEEPALIVE，帮助维持 NAT/防火墙映射；
    但这是传输层探活，对应用层 L7 负载均衡的 idle timeout 无效——那种只能靠流式。
  - 超时采用 (连接超时, 读取超时) 元组：连接快速失败，读取阶段给足生图时间。

可选枚举参数用 "default" 档表示「不发送该字段、由服务端用默认值」，避免给不支持
该字段的网关塞未知参数导致 400。结果从 data[].b64_json (GPT image 模型默认) 或
data[].url (DALL-E 风格) 读取；流式结果从 *.completed 事件的 b64_json 读取。
"""

import base64
import http.client
import io
import json
import os
import re
import socket
import threading
import time

import numpy as np
import requests
import torch
import urllib3.exceptions
from PIL import Image
from urllib3.connection import HTTPConnection

# 可选枚举参数的合法取值（第一项 default = 不发送）。
QUALITY_OPTIONS = ["default", "auto", "high", "medium", "low"]
BACKGROUND_OPTIONS = ["default", "auto", "transparent", "opaque"]
OUTPUT_FORMAT_OPTIONS = ["default", "png", "jpeg", "webp"]
MODERATION_OPTIONS = ["default", "auto", "low"]
# 输入保真度 (input_fidelity)：仅 /images/edits 有意义；default = 不发送。
INPUT_FIDELITY_OPTIONS = ["default", "low", "high"]
# 返回格式 (response_format)：**默认必须是 default(不发送)**。
# 官方 GPT-Image 系列**不支持**该字段——文档明确「This parameter isn't supported for
# the GPT image models, which always return base64-encoded images.」，且实测在
# /images/edits 上发它会 400，报错还误指向 `model`（"Value must be 'dall-e-2'"），
# 极难排查。所以绝不能默认发送。
# 但不少兼容网关**不返回内联 base64、而是把成品图放自家图床再回 url**，那个图床在
# 高峰期常抖(504)，导致「图生成好了却下载失败」。对这类网关，显式发
# response_format=b64_json 可让图片内联返回、完全绕开图床(实测有效)。
# 故设计成手动开关：默认 default 保证对官方与严格网关安全，需要时再开。
RESPONSE_FORMAT_OPTIONS = ["default", "b64_json", "url"]

# 连接建立超时(秒)；读取超时由调用方按生图时长传入(可能很久)。
CONNECT_TIMEOUT = 15

# ── 模型能力表 (model capability table)：参数与模型的联动依据 ──────────────
# 只有「已知官方模型」才做硬校验(违规直接报错、省一次昂贵/缓慢的 API 往返)；
# 未知模型名(自定义 OpenAI 兼容网关)一律软放行(仅打印警告),以保住兼容性。
# 规则来源：OpenAI Images API 对各 GPT-Image 模型的官方约束。
#   size          -> "strict"：gpt-image-2 的严格尺寸约束；"legacy"：固定几档尺寸
#   transparent   -> 是否支持透明背景 (background=transparent)
#   input_fidelity-> 是否支持 input_fidelity 字段 (gpt-image-2 恒为 high，不接受该字段)
MODEL_RULES = {
    "gpt-image-2":      {"size": "strict", "transparent": False, "input_fidelity": False},
    "gpt-image-1.5":    {"size": "legacy", "transparent": True,  "input_fidelity": True},
    "gpt-image-1":      {"size": "legacy", "transparent": True,  "input_fidelity": True},
    "gpt-image-1-mini": {"size": "legacy", "transparent": True,  "input_fidelity": True},
}

# gpt-image-2 尺寸约束 (与官方一致)。
GPT_IMAGE_2_MIN_PIXELS = 655_360
GPT_IMAGE_2_MAX_PIXELS = 8_294_400
GPT_IMAGE_2_MAX_EDGE = 3840
GPT_IMAGE_2_MAX_RATIO = 3.0
# legacy 模型 (gpt-image-1.x) 只接受这几档 size。
ALLOWED_LEGACY_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}

# 可重试的 HTTP 状态码：限流 + 常见网关/上游临时故障。
# 520~527 是 Cloudflare 边缘自有的一段 5xx（"Web server is returning an unknown
# error" / 源站超时等）。很多 OpenAI 兼容网关挂在 Cloudflare 后面，这段码表示
# **边缘到源站之间**出了问题，与本次请求的参数无关，属于典型的瞬时故障，值得重试。
RETRYABLE_STATUS = {429, 500, 502, 503, 504,
                    520, 521, 522, 523, 524, 525, 526, 527}
# 单次退避封顶(秒)，避免 Retry-After 过大时无谓长睡。
MAX_BACKOFF = 60.0

# 放进异常消息的上游正文上限。取 2000 而非 500：500 字连一页 Cloudflare 错误页的
# <head> 都装不满，真正有用的 JSON error.message 常被切掉，导致「看到报错却无法排查」。
# 同时错误正文会**完整**打进 ComfyUI 控制台日志（见 _http_error_message），
# 异常框里只放提炼后的摘要，避免整页 HTML 淹没 traceback。
MAX_ERROR_BODY = 2000

# ComfyUI 中断机制 (interrupt)：懒加载，在 ComfyUI 外(单测/test_api 等独立运行)
# 时 ImportError 降级为 None，_check_interrupt() 变成 no-op，不影响独立调用。
# ComfyUI 的中断是协作式轮询：节点必须主动调用
# comfy.model_management.throw_exception_if_processing_interrupted() 才能响应
# 用户点击 Cancel；单次阻塞的 requests.post() 无法被打断，需靠 daemon 线程 + 轮询。
try:
    import comfy.model_management as _comfy_mm
except ImportError:
    _comfy_mm = None

_SESSION = None


def _session():
    """返回带 TCP keepalive 的共享 requests.Session（懒加载单例）。"""
    global _SESSION
    if _SESSION is not None:
        return _SESSION

    opts = list(HTTPConnection.default_socket_options)  # 含 TCP_NODELAY
    opts.append((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1))
    # 平台相关的探测参数：常量不存在的平台自动跳过。
    for attr, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 30),
                        ("TCP_KEEPCNT", 4), ("TCP_KEEPALIVE", 60)):
        const = getattr(socket, attr, None)
        if const is not None:
            opts.append((socket.IPPROTO_TCP, const, value))

    class _KeepAliveAdapter(requests.adapters.HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            kwargs["socket_options"] = opts
            return super().init_poolmanager(*args, **kwargs)

    sess = requests.Session()
    adapter = _KeepAliveAdapter()
    sess.mount("http://", adapter)
    sess.mount("https://", adapter)
    _SESSION = sess
    return _SESSION


def _check_interrupt():
    """若 ComfyUI 已发出取消信号则抛出 InterruptProcessingException；独立运行时 no-op。

    InterruptProcessingException 继承 BaseException（不是 Exception），可穿透
    调用链上所有的 except Exception 块，确保信号沿栈向上传播到 ComfyUI 执行器。
    永远不要在自定义节点中 catch BaseException / InterruptProcessingException。
    """
    if _comfy_mm is not None:
        _comfy_mm.throw_exception_if_processing_interrupted()


def _interruptible_post(url, *, headers, timeout, stream, **req_kw):
    """把 requests.post 放进 daemon 线程，主线程每 500ms 轮询 ComfyUI 中断标志。

    根因：ComfyUI 使用「协作式轮询 (cooperative polling)」中断机制——节点须主动
    调用 throw_exception_if_processing_interrupted() 才能响应 Cancel。单次阻塞的
    requests.post() 将线程挂在 OS 网络层，期间无任何 Python 代码执行，中断标志
    永远无法被读到。对十几分钟的生图任务来说形同虚设。

    修复方案：daemon 线程做 HTTP，主线程每 500ms 轮询一次中断标志。
    中断时 InterruptProcessingException(BaseException) 立即沿栈向上传播；
    daemon 线程继续在后台完成（或等待超时），不阻止 ComfyUI 继续运行。
    caller 的 except requests.RequestException 不会误捕获 BaseException 子类。
    """
    result = [None]
    error = [None]
    done = threading.Event()

    def _worker():
        try:
            result[0] = _session().post(
                url, headers=headers,
                timeout=(CONNECT_TIMEOUT, timeout), stream=stream, **req_kw)
        except Exception as exc:
            error[0] = exc
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    while not done.wait(timeout=0.5):
        _check_interrupt()   # 每 500ms 检查一次；中断时立即抛 InterruptProcessingException
    _check_interrupt()       # 线程结束后再检查一次（done.set 前中断信号可能刚到）
    if error[0] is not None:
        raise error[0]
    return result[0]


def tensor_to_png_bytes(image_tensor):
    """单张 ComfyUI IMAGE tensor [H,W,C] (0-1 float) -> PNG bytes。"""
    arr = (image_tensor.cpu().numpy() * 255.0).clip(0, 255).astype(np.uint8)
    pil = Image.fromarray(arr)
    buf = io.BytesIO()
    pil.save(buf, format="PNG")
    return buf.getvalue()


def bytes_to_tensor(img_bytes):
    """原始图片字节 -> ComfyUI IMAGE tensor [1,H,W,3] float 0-1。"""
    pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    arr = np.array(pil).astype(np.float32) / 255.0
    return torch.from_numpy(arr)[None, ...]


def mask_to_png_bytes(mask_tensor):
    """ComfyUI MASK tensor -> RGBA PNG bytes（供 /images/edits 的 mask 字段）。

    OpenAI 约定：mask 中透明(alpha=0)的区域会被编辑，不透明处保留。
    ComfyUI 的 MASK 里 1.0 表示选中(要编辑)的区域，故 alpha = (1 - mask) * 255。
    """
    m = mask_tensor
    if hasattr(m, "dim") and m.dim() == 3:  # [B,H,W] 取第一张
        m = m[0]
    arr = m.cpu().numpy().astype(np.float32)
    alpha = ((1.0 - arr).clip(0.0, 1.0) * 255.0).astype(np.uint8)
    h, w = alpha.shape
    rgba = np.zeros((h, w, 4), dtype=np.uint8)
    rgba[..., 3] = alpha
    buf = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buf, format="PNG")
    return buf.getvalue()


def _clean(v):
    return str(v).strip() if v is not None else ""


def _coerce_int(v):
    """尽力把 v 转成 int；转不了返回 None（不抛，交给调用方决定怎么办）。

    ComfyUI 的 INT widget 在部分前端版本会把值交成 float（1 -> 1.0），
    甚至字符串（"1"）。这里统一收口成真正的 int，避免这类值一路带到请求里。
    """
    if isinstance(v, bool) or v is None:
        return None
    try:
        return int(float(v)) if isinstance(v, str) else int(v)
    except (TypeError, ValueError):
        return None


def _form_value(v):
    """把参数值转成 multipart 表单字段该有的字符串写法。

    为什么需要它（两个端点的编码差异 / why this exists）：
      - /images/generations 走 JSON，`{"n": 1}` 里的 1 是**真正的整数类型**；
      - /images/edits 走 multipart/form-data，协议上**所有字段值都是字符串**，
        没有数字类型可言。服务端必须自己把 "1" 解析回整数。
    因此这一层要给出「最无争议的字符串写法」，不能直接用 python 的 str()：
      - bool  -> "true"/"false"（str(True) 得到 "True"，多数网关不认）
      - int   -> 十进制数字，无小数点
      - float -> 整数值去掉 ".0"（str(1.0) 得到 "1.0"，严格按整数校验的网关
                 会判成「不是整数」而回 400 INVALID_PARAM）
    """
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return "%d" % v
    if isinstance(v, float):
        return "%d" % int(v) if v.is_integer() else repr(v)
    return str(v)


def _normalize_base_url(base_url):
    base = _clean(base_url).rstrip("/")
    if not base:
        raise ValueError("[GPT-Image] base_url(接口地址) 为空，请在「GPT-Image API 配置」节点里填写。")
    if not (base.startswith("http://") or base.startswith("https://")):
        raise ValueError("[GPT-Image] base_url 必须以 http:// 或 https:// 开头，当前为: %r" % base_url)
    return base


def _auth_headers(api_key):
    key = _clean(api_key)
    if not key:
        raise ValueError("[GPT-Image] api_key(密钥) 为空，请在「GPT-Image API 配置」节点里填写。")
    # 密钥只放在 Authorization 头里，绝不写进日志。
    return {"Authorization": "Bearer " + key}


def size_from_wh(width, height):
    """宽/高 -> OpenAI size 字符串。两者都 >0 时拼 "宽x高"，否则 "auto"。

    不强制圆整用户填的值；尺寸合法性交给 build_params 里的 _validate 按模型判断
    （已知模型硬校验、未知模型软警告），最终由服务端裁决。
    """
    try:
        w = int(width or 0)
        h = int(height or 0)
    except (TypeError, ValueError):
        return "auto"
    if w <= 0 or h <= 0:
        return "auto"
    return "%dx%d" % (w, h)


def snap_dim(value, step=16, lo=16, hi=None):
    """把单条边 value 规范化：圆整到最近的 step 倍数，并 clamp 到 [lo, hi]。

    端点也对齐到 step 倍数（lo 向上取、hi 向下取），保证结果既是 step 的倍数、
    又落在 [lo, hi] 内。hi 缺省用 gpt-image-2 的最长边 3840。
    供「尺寸规范化」节点把用户随手填的宽/高吸附成合法值；比例(1:3~3:1)与
    总像素约束不在这里处理，仍由 _validate 在发请求前校验。
    """
    step = max(1, int(step))
    if hi is None:
        hi = GPT_IMAGE_2_MAX_EDGE
    v = (int(round(value)) + step // 2) // step * step   # 逢中向上圆整到 step 倍数
    lo_s = -(-int(lo) // step) * step                    # ceil(lo/step)*step
    hi_s = int(hi) // step * step                        # floor(hi/step)*step
    if hi_s < lo_s:                                      # 参数打架时以下限为准
        hi_s = lo_s
    return max(lo_s, min(v, hi_s))


def unpack_config(config):
    """从配置节点的输出里取出 (base_url, api_key)。"""
    if not config or not isinstance(config, (tuple, list)) or len(config) < 2:
        raise ValueError("[GPT-Image] 未提供有效配置，请连接「GPT-Image API 配置」节点到「配置」输入。")
    return config[0], config[1]


# ── 本地配置文件（免连线模式）────────────────────────────────────────
# 生成/编辑节点**未连**「配置」输入时，自动从这里读 (base_url, api_key)：
# 配一次、全插件生效，不用每个节点都去连「GPT-Image API 配置」节点。
# 文件名带 local_ 前缀并写入 .gitignore，明确「本机私有、不进版本库、
# 不随工作流分享」。校验与配置节点共用 validate_credentials，两条路行为一致。
FILE_CONFIG_NAME = "local_config.json"


def _config_file_path():
    """配置文件路径：插件根目录（以本文件所在目录为基准，不依赖 cwd）。"""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), FILE_CONFIG_NAME)


def validate_credentials(base_url, api_key):
    """校验 (base_url, api_key) 并归一化；非法抛 ValueError。

    配置节点 (config_node.build) 与本地配置文件共用这一个校验入口，
    保证「配置节点连线」和「免连线配置文件」两条路行为一致。
    """
    base_url = (base_url or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    if not base_url:
        raise ValueError("[GPT-Image] 接口地址(base_url) 不能为空，请填写你自己的 API 地址，"
                         "例如 https://your-endpoint.example.com/v1")
    if not (base_url.startswith("http://") or base_url.startswith("https://")):
        raise ValueError("[GPT-Image] 接口地址必须以 http:// 或 https:// 开头。")
    if not api_key:
        raise ValueError("[GPT-Image] 密钥(api_key) 不能为空。")
    return base_url, api_key


def load_file_config(path=None):
    """从 local_config.json 读取 (base_url, api_key)；没有/读不了返回 None。

    每次调用实时读文件：改配置即生效，无需重启 ComfyUI。文件不存在、JSON
    损坏、缺字段都只打印警告并返回 None——绝不抛异常拖垮节点，之后由
    resolve_credentials 给出明确的中文报错。path 参数仅供测试注入。
    """
    p = path or _config_file_path()
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        base_url = (data.get("base_url") or "").strip().rstrip("/")
        api_key = (data.get("api_key") or "").strip()
        if not base_url or not api_key:
            print("[GPT-Image] 配置文件 %s 缺少 base_url 或 api_key，已忽略。" % p)
            return None
        return base_url, api_key
    except ValueError as e:          # JSON 语法错误
        print("[GPT-Image] 配置文件 %s 解析失败(JSON 损坏?): %s，已忽略。" % (p, e))
        return None
    except OSError as e:             # 读不到
        print("[GPT-Image] 无法读取配置文件 %s: %s，已忽略。" % (p, e))
        return None


def resolve_credentials(config):
    """解析节点凭据：优先「配置」节点连线，其次本地配置文件，都没有则报错。

    返回 (base_url, api_key)。显式连线 > 配置文件：连了「配置」节点就完全用
    配置节点的值，配置文件只做免连线兜底——旧工作流行为不受任何影响。
    """
    if config:
        return unpack_config(config)
    file_cfg = load_file_config()
    if file_cfg:
        return file_cfg
    raise ValueError(
        "[GPT-Image] 未提供 API 凭据，请二选一：\n"
        "  ① 连接「GPT-Image API 配置」节点到「配置」输入；\n"
        "  ② 在插件目录创建 %s 填写 base_url 与 api_key（免连线，配一次全插件生效）。"
        % _config_file_path())


def _parse_size(size):
    """"宽x高" -> (w, h)；不匹配返回 None（含 "auto"）。"""
    s = _clean(size)
    if not s or s == "auto":
        return None
    parts = s.lower().split("x")
    if len(parts) != 2:
        return "invalid"
    try:
        w, h = int(parts[0]), int(parts[1])
    except ValueError:
        return "invalid"
    if w <= 0 or h <= 0:
        return "invalid"
    return (w, h)


def _check_gpt_image_2_size(size, strict):
    """gpt-image-2 尺寸约束校验。strict=True 时违规 raise，否则 print 警告。"""
    parsed = _parse_size(size)
    if parsed is None:  # auto / 未指定
        return
    fail = _die if strict else _warn
    if parsed == "invalid":
        fail("size 必须是 auto 或 宽x高（如 1024x1024），当前: %r" % size)
        return
    w, h = parsed
    max_edge, min_edge = max(w, h), min(w, h)
    total = w * h
    if w % 16 or h % 16:
        fail("gpt-image-2 要求宽高均为 16 的倍数，当前 %dx%d。" % (w, h))
    elif max_edge > GPT_IMAGE_2_MAX_EDGE:
        fail("gpt-image-2 最长边不得超过 %dpx，当前 %dpx。" % (GPT_IMAGE_2_MAX_EDGE, max_edge))
    elif max_edge / min_edge > GPT_IMAGE_2_MAX_RATIO:
        fail("gpt-image-2 长短边比不得超过 3:1，当前 %dx%d。" % (w, h))
    elif total < GPT_IMAGE_2_MIN_PIXELS or total > GPT_IMAGE_2_MAX_PIXELS:
        fail("gpt-image-2 总像素需在 %d~%d 之间，当前 %d。"
             % (GPT_IMAGE_2_MIN_PIXELS, GPT_IMAGE_2_MAX_PIXELS, total))


def _die(msg):
    raise ValueError("[GPT-Image] " + msg)


def _warn(msg):
    print("[GPT-Image] 提示(自定义网关，仅警告不拦截)：" + msg)


def _validate(params):
    """参数×模型联动校验（单一入口）。已知官方模型硬校验、未知模型软警告。

    会就地修改 params：对不支持 input_fidelity 的模型丢弃该字段（打印说明）。
    """
    model = params.get("model", "")
    rule = MODEL_RULES.get(model)          # None = 未知模型/自定义网关
    strict = rule is not None
    size = params.get("size", "auto")
    bg = params.get("background")
    fmt = params.get("output_format")

    # 1) 尺寸：按模型分流
    if rule and rule["size"] == "strict":
        _check_gpt_image_2_size(size, strict=True)
    elif rule and rule["size"] == "legacy":
        if _clean(size) and size not in ALLOWED_LEGACY_SIZES:
            _die("%s 只支持 size ∈ {1024x1024, 1536x1024, 1024x1536, auto}，当前: %r" % (model, size))
    else:
        # 未知模型：只做轻量的 16 倍数软提示，不拦截（网关可能支持任意尺寸）。
        parsed = _parse_size(size)
        if isinstance(parsed, tuple) and (parsed[0] % 16 or parsed[1] % 16):
            _warn("gpt-image 系列通常要求宽高为 16 的倍数，当前 %dx%d 可能被拒。" % parsed)

    # 2) 透明背景联动
    if bg == "transparent":
        if fmt == "jpeg":
            _die("透明背景(transparent) 需要 png/webp 输出，jpeg 无法保留透明通道。")
        if rule and not rule["transparent"]:
            _die("%s 不支持透明背景。请改用 --模型 gpt-image-1.5 并设 输出格式=png/webp。" % model)

    # 3) input_fidelity 联动：模型不支持时丢弃（不报错，避免 edit 配置摩擦）
    if params.get("input_fidelity") and rule and not rule["input_fidelity"]:
        print("[GPT-Image] %s 忽略 input_fidelity（该模型输入始终高保真）。" % model)
        params.pop("input_fidelity", None)

    # 4) response_format：官方 GPT-Image 系列不支持该字段（文档明确「always return
    #    base64-encoded images」），实测在官方 /images/edits 上发它会 400、且报错误
    #    指向 `model`（"Value must be 'dall-e-2'"），极难排查。
    #    但**这里只警告、不丢弃**：模型名不足以判断对端是官方还是兼容网关——实测有
    #    网关自报 model=gpt-image-2 却返回 url(官方从不返回 url)、并正常接受
    #    response_format。对这类网关，显式发 b64_json 正是绕开不稳图床的唯一手段。
    #    该字段默认是 default(不发送)，出现在这里说明用户主动选了，遵从用户判断。
    if params.get("response_format") and strict:
        print("[GPT-Image] 注意：%s 按官方文档**不支持** response_format（官方恒返回 "
              "base64，发送可能回 400 且报错误指向 model）。仅当你的网关只回 url、"
              "需要强制内联 base64 时才保留此项；连官方接口请设回 default。" % model)

    return params


def build_params(model, prompt, size="auto", n=1, quality="default",
                 background="default", output_format="default",
                 output_compression=None, moderation="default",
                 input_fidelity="default", response_format="default"):
    """构造两个端点共用的参数字典。枚举取 "default" 时不发送该字段。

    n（数量）沿用同一套「不发送」约定：n <= 0 或无法解析成整数时**不发送 n**，
    由服务端用它自己的默认值（通常是 1）。这既符合本仓库「default 档 = 不发送
    该字段」的设计不变量，也是遇到网关对 n 校验异常时的逃生口——个别 OpenAI
    兼容网关在 multipart 的 /images/edits 上会错判 n（协议上 n 只能是字符串
    "1"，网关若按整数强校验就会回 `INVALID_PARAM: n 必须是 … 整数`），
    此时把「数量」设为 0 即可整字段不发。

    组装完成后调用 _validate 做参数×模型联动校验（单一校验入口）。
    返回的是「标准 python 值」的 dict：generations 直接当 JSON body（int 保持
    int）；edits 会在 edit_images 里逐个经 _form_value 转成 multipart 字符串。
    """
    prompt = _clean(prompt)
    if not prompt:
        raise ValueError("[GPT-Image] 提示词(prompt) 为空。")
    params = {"model": _clean(model) or "gpt-image-2", "prompt": prompt}

    n_val = _coerce_int(n)
    if n_val is not None and n_val > 0:
        params["n"] = n_val

    size = _clean(size)
    if size:
        params["size"] = size

    for key, val in (
        ("quality", quality),
        ("background", background),
        ("output_format", output_format),
        ("moderation", moderation),
        ("input_fidelity", input_fidelity),
        ("response_format", response_format),
    ):
        v = _clean(val)
        if v and v != "default":
            params[key] = v

    # output_compression 只在输出 jpeg/webp 时有意义。
    if params.get("output_format") in ("jpeg", "webp") and output_compression is not None:
        c = _coerce_int(output_compression)
        if c is not None and 0 <= c <= 100:
            params["output_compression"] = c

    return _validate(params)


def _download_image(url, timeout, attempts, index=0, total=1):
    """下载一张结果图，对瞬时错误重试。返回图片字节。

    为什么必须重试(而不是像早先那样一次失败就整体报错)：很多兼容网关不返回内联
    base64，而是把成品图先放自家图床再回 url。**这一步与生图是两套服务**——图床在
    高峰期抖动(504/连接重置)非常常见，实测同一个 url 首次 504、1.5s 后重试即 200。
    而此时图已经生成、已经计费，若不重试就整批丢弃，等于白花钱。`数量(n)>1` 时更
    致命：每多一张就多一次抖动机会，任一张失败会连带丢掉其余已下载成功的图。
    """
    last = None
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            r = _session().get(url, timeout=(CONNECT_TIMEOUT, timeout))
        except requests.RequestException as e:
            last = "%s: %s" % (e.__class__.__name__, e)
        else:
            if r.status_code == 200:
                return r.content
            last = "HTTP %s" % r.status_code
            # 非瞬时错误(如 403/404 签名过期)重试无意义，直接失败。
            if r.status_code not in RETRYABLE_STATUS:
                attempt = attempts
            r.close()
        if attempt >= attempts:
            break
        wait = min(MAX_BACKOFF, _backoff_seconds(attempt))
        print("[GPT-Image] 第 %d/%d 张结果图下载失败(%s)，%.1fs 后重试。"
              % (index + 1, total, last, wait))
        time.sleep(wait)
        _check_interrupt()
    raise RuntimeError(
        "[GPT-Image] 下载第 %d/%d 张结果图失败(已试 %d 次): %s\n"
        "  说明: 图片已由上游生成(可能已计费)，失败发生在**从网关图床取图**这一步，"
        "与提示词/参数无关。\n"
        "  提示: 这类抖动多为图床临时故障——可加大「重试次数」；若网关支持，"
        "把「返回格式」设为 b64_json 让图片内联返回，即可完全绕开图床。\n"
        "  url=%s" % (index + 1, total, max(1, int(attempts)), last, url))


def _images_from_response(resp_json, timeout, attempts=1):
    """OpenAI ImagesResponse -> list of ComfyUI IMAGE tensors [1,H,W,3]。

    优先读内联的 base64 (b64_json，GPT image 模型默认返回)；否则回退到 url
    (DALL-E 风格)。url 由服务端返回，只有服务端确实返回时才会去取。
    """
    data = (resp_json or {}).get("data") or []
    if not data:
        # 200 但没有图：可能是网关把错误塞进了 200 响应，故沿用同一套提炼逻辑。
        # 实测确有网关在限流时回 200 + 错误正文而非 429，故不能只看状态码。
        detail = _dig_error_message(resp_json) or _collapse(str(resp_json))[:MAX_ERROR_BODY]
        raise RuntimeError("[GPT-Image] 响应里没有图片数据 (data 为空): %s" % detail)
    tensors = []
    for i, item in enumerate(data):
        b64 = item.get("b64_json")
        if b64:
            tensors.append(bytes_to_tensor(base64.b64decode(b64)))
            continue
        url = item.get("url")
        if url:
            tensors.append(bytes_to_tensor(
                _download_image(url, timeout, attempts, index=i, total=len(data))))
            continue
        raise RuntimeError("[GPT-Image] 结果项既无 b64_json 也无 url: %s" % str(item)[:300])
    return tensors


# ── SSE 事件类型 (documented event enum) ────────────────────────────────────
# 官方只定义了这 4 个 type 值，**没有**定义任何错误事件：
#   https://developers.openai.com/api/reference/resources/images/generation-streaming-events
#   https://developers.openai.com/api/reference/resources/images/edit-streaming-events/
# 故解析策略分两层，优先级明确：
#   1) 规范内事件 -> 对这个闭集**精确匹配**（不做子串猜测）。
#   2) 流内错误   -> 规范外的网关扩展。既然无规范可依，就用**结构**判定
#      (存在非空 error 对象)，而不是在 type 里找 "error" 这类关键字。
PARTIAL_EVENTS = frozenset(("image_generation.partial_image", "image_edit.partial_image"))
COMPLETED_EVENTS = frozenset(("image_generation.completed", "image_edit.completed"))
# 规范外但实测存在的错误事件名（各网关自造）。仅作为**精确**匹配集，不做子串匹配。
ERROR_EVENTS = frozenset(("error", "image_generation.failed", "image_edit.failed",
                          "response.failed", "failed"))


def _sse_events(resp):
    """按 SSE 规范 (W3C/WHATWG server-sent events) 解析事件流。

    此前的实现只扫 `data:` 行、且把每行当成一个完整事件，这偏离规范两处：
      - 规范允许**同一事件的 data 跨多行**，须按出现顺序用 "\\n" 拼接，
        遇空行(dispatch)才算一个事件结束。图片 base64 很长，一旦上游分多行发，
        逐行 json.loads 会全部解析失败 -> 表现为「流里没拿到图片」。
      - 事件**名**规范上在 `event:` 字段里；本 API 的事件名同时也放在 JSON 的
        `type` 字段。故这里两个都取，以 `event:` 为先、`type` 兜底。
    以 `:` 开头的行是注释/心跳(如 `: ping`)，按规范忽略——它正是保活用的。

    yield (event_name, data_str)；不解析 JSON，交给调用方。
    """
    event_name = ""
    data_lines = []
    for raw in resp.iter_lines(decode_unicode=True):
        _check_interrupt()   # 流式迭代间隙检查中断，比单次阻塞更及时
        if raw is None:
            continue
        line = raw.rstrip("\r")
        if line == "":                       # 空行 = dispatch 事件
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = ""
            data_lines = []
            continue
        if line.startswith(":"):             # 注释/心跳，规范要求忽略
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):            # 规范：冒号后**单个**前导空格要去掉
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
        # id / retry 字段本 API 用不到，按规范忽略即可
    if data_lines:                           # 流尾没有空行时也要 dispatch
        yield event_name, "\n".join(data_lines)


def _stream_error_message(obj):
    """从 SSE 事件里识别「流内错误事件」，返回可读消息；不是错误事件则返回 ""。

    官方规范**没有**定义错误事件（只有 partial_image / completed），但实测上游确会
    在 HTTP 200 之后于流内推 `{"type":"error","error":{...}}`（审核拒绝、上游崩溃）。
    早先的解析器「没有 b64_json 就 continue」会把它整个吞掉，最终只报一句「没拿到
    图片」，真正的原因(如违规拦截)完全看不到。

    既然无规范可依，判定就以**结构**为准、而非关键字：
      1) 存在非空 `error` 对象/字符串 -> 是错误事件（最可靠，与事件名无关）；
      2) 或 type 精确命中 ERROR_EVENTS（各网关自造的错误事件名）。
    刻意**不再**用 `"error" in type` 这类子串匹配：那会把未来任何名字里带 error 的
    正常事件误判成失败，把「已出图」谎报成「失败」。
    """
    if not isinstance(obj, dict):
        return ""
    err = obj.get("error")
    has_err_obj = (isinstance(err, dict) and bool(err)) or \
                  (isinstance(err, str) and bool(err.strip()))
    etype = str(obj.get("type", "") or "")
    if not has_err_obj and etype not in ERROR_EVENTS:
        return ""
    msg = _dig_error_message(obj)
    if msg:
        extra = [str(obj[k]) for k in ("code", "param") if obj.get(k)]
        return msg + ("  [%s]" % ", ".join(extra) if extra else "")
    # 认定为错误却挖不出消息时，绝不能返回 "" —— 那会让调用方以为「不是错误」而继续，
    # 最终把真正的失败原因丢掉。原样回传截断后的事件内容。
    return _collapse(str(obj))[:MAX_ERROR_BODY] or "(错误事件，但无可读消息)"


def _images_from_stream(resp):
    """解析 SSE 流：累积 partial 预览，返回最终 completed 的图 [1,H,W,3]。

    事件类型按官方枚举**精确**匹配（见 PARTIAL_EVENTS / COMPLETED_EVENTS）；
    对未知事件名(兼容网关自造)保留「有 b64_json 就当成品」的兜底，否则整条流白丢。
    n>1 时按 completed 事件出现顺序收集**多张**成品图：规范未定义多图流式语义
    (completed 事件上没有图序号字段，只有 partial 事件有 partial_image_index)，
    故不依赖任何索引字段，只按到达顺序追加，天然兼容「一图一 completed」。
    """
    finals = []
    last_partial = None
    partial_count = 0
    unknown_types = []
    for event_name, payload in _sse_events(resp):
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        # 错误事件优先：HTTP 已经 200，真正的失败原因只在流里，必须抛出而非吞掉。
        err = _stream_error_message(obj)
        if err:
            raise RuntimeError(
                "[GPT-Image] 流式响应中上游报错: %s\n"
                "  说明: HTTP 状态码是 200，错误在 SSE 流内推送，与网络无关，"
                "重试通常无用。" % err)
        # 事件名：规范在 `event:` 字段，本 API 同时放在 JSON 的 type，两者都认。
        etype = event_name or str(obj.get("type", "") or "")
        b64 = obj.get("b64_json")
        if etype in COMPLETED_EVENTS:
            if b64:
                finals.append(b64)
            continue
        if etype in PARTIAL_EVENTS:
            if b64:
                partial_count += 1
                last_partial = b64
                print("[GPT-Image] 流式预览 #%s 已接收（保活中）"
                      % obj.get("partial_image_index", partial_count - 1))
            continue
        # 规范外事件名：只要带成品图就收下（兼容自造事件名的网关），否则记下备诊断。
        if b64:
            finals.append(b64)
        elif etype:
            unknown_types.append(etype)
    if finals:
        return [bytes_to_tensor(base64.b64decode(b)) for b in finals]
    # 只有 partial、没有 completed：流被中途掐断(代理超时/上游崩溃)。绝不能把
    # 低清预览图当成品静默返回——那会让「生成失败」伪装成「生成了一张糊图」。
    if last_partial is not None:
        raise RuntimeError(
            "[GPT-Image] 流式响应只收到预览图(partial_image)、没有收到成品图"
            "(completed)：流在完成前被中断（常见于中间代理空闲超时或上游崩溃）。\n"
            "  说明: 已收到 %d 个预览事件，故请求确实到达并开始生成；缺的是最后一步。"
            "上游可能已计费。\n"
            "  提示: 可加大「超时秒数」；若反复如此，关掉「流式」改用非流式，"
            "或联系网关方。" % partial_count)
    raise RuntimeError(
        "[GPT-Image] 流式响应里没有拿到图片（无 completed/partial 事件）。%s\n"
        "  提示: 该网关可能并未真正实现 SSE 流式(有的网关会忽略 stream 参数)，"
        "可关掉「流式」重试。"
        % ("收到的未知事件类型: %s。" % ", ".join(sorted(set(unknown_types))[:8])
           if unknown_types else ""))


def _result_tensors(resp, timeout, stream, attempts=1):
    """按响应类型解析：SSE 走流式解析；否则(含网关未按 SSE 返回)走普通 JSON。

    注意「请求了 stream 但响应不是 event-stream」是常见情况(不少兼容网关直接忽略
    stream 参数，实测本仓库测过的网关即如此)，此时按普通 JSON 解析、并把下载重试
    次数一并传下去。
    """
    if stream and "text/event-stream" in resp.headers.get("Content-Type", ""):
        return _images_from_stream(resp)
    return _images_from_response(resp.json(), timeout, attempts)


def _retry_after_seconds(resp):
    """从响应的 Retry-After 头解析等待秒数；解析不出(如 HTTP-date)返回 None。"""
    if resp is None:
        return None
    val = resp.headers.get("Retry-After")
    if not val:
        return None
    try:
        return max(0.0, float(val))
    except (TypeError, ValueError):
        return None


def _backoff_seconds(attempt):
    """指数退避：2^attempt 秒，封顶 MAX_BACKOFF。"""
    return min(MAX_BACKOFF, 2.0 ** attempt)


# ── 上游错误的可读化 (making upstream errors diagnosable) ────────────────────
# 背景：早先错误信息是 `r.text[:500]`。网关挂在 Cloudflare 后面时，正文是一整页
# HTML 错误页，前 500 字全是 <!DOCTYPE>/条件注释/<meta>，真正有用的一句话
# (标题里的 "520: Web server is returning an unknown error") 恰好被切在外面；
# JSON 错误也可能因为前面裹了一层而被截断。结果是「有报错但没法排查」。
# 现在：从正文里**提炼**关键信息(JSON 的 error.message / HTML 的 <title> + 正文
# 文字)，附上响应侧诊断头与本次实际发送的字段，并把完整正文打进 ComfyUI 日志。

_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1\s*>")
_TAG_RE = re.compile(r"(?s)<[^>]*>")
_TITLE_RE = re.compile(r"(?is)<title[^>]*>(.*?)</title>")
# 排查时有用、且不含敏感信息的响应头（密钥只在请求头里，不会出现在响应里）。
_DIAG_HEADERS = ("x-request-id", "request-id", "x-trace-id", "x-amzn-requestid",
                 "cf-ray", "retry-after", "server", "date")


def _collapse(s):
    """把连续空白压成单个空格，便于单行展示。"""
    return re.sub(r"\s+", " ", s or "").strip()


def _looks_like_html(body, content_type):
    head = body[:300].lstrip().lower()
    return ("html" in (content_type or "").lower()
            or head.startswith("<!doctype") or head.startswith("<html")
            or "<html" in head)


def _html_summary(html):
    """HTML 错误页 -> 一行摘要：优先 <title>，再补正文可见文字。"""
    title = ""
    m = _TITLE_RE.search(html)
    if m:
        title = _collapse(_TAG_RE.sub("", m.group(1)))
    text = _collapse(_TAG_RE.sub(" ", _SCRIPT_RE.sub(" ", html)))
    if title:
        # 正文往往以标题原文开头，去掉重复部分只留后续说明。
        rest = text[len(title):] if text.startswith(title) else text
        return _collapse(title + " | " + rest[:400]) if rest.strip() else title
    return text[:400] or "(HTML 错误页，无可提取文字)"


def _dig_error_message(obj):
    """从常见的错误 JSON 形状里挖出人类可读的一句话。

    覆盖 OpenAI 的 {"error": {"message", "code", "type"}}、各类兼容网关的
    {"message"} / {"detail"} / {"msg"} / {"error": "..."} 等写法。
    """
    if not isinstance(obj, dict):
        return ""
    err = obj.get("error")
    if isinstance(err, dict):
        msg = _clean(err.get("message") or err.get("msg") or err.get("detail"))
        extra = [str(err[k]) for k in ("code", "type", "param") if err.get(k)]
        if msg:
            return msg + ("  [%s]" % ", ".join(extra) if extra else "")
    elif isinstance(err, str) and err.strip():
        return err.strip()
    for key in ("message", "detail", "msg", "error_msg", "errorMessage"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _extract_error_message(body, content_type=""):
    """上游错误正文 -> 提炼后的关键信息（JSON 消息 / HTML 摘要 / 纯文本）。"""
    if not (body or "").strip():
        return "(上游返回空正文)"
    s = body.strip()
    if s[:1] in ("{", "["):
        try:
            msg = _dig_error_message(json.loads(s))
        except ValueError:
            msg = ""
        if msg:
            return msg[:MAX_ERROR_BODY]
    if _looks_like_html(s, content_type):
        return _html_summary(s)[:MAX_ERROR_BODY]
    return _collapse(s)[:MAX_ERROR_BODY]


def _response_diag(resp):
    """响应侧诊断串：去掉 query 的 URL + Content-Type + 可用于报障的追踪头。"""
    bits = []
    url = getattr(resp, "url", "") or ""
    if url:
        bits.append("url=" + url.split("?", 1)[0])
    ctype = resp.headers.get("Content-Type")
    if ctype:
        bits.append("content-type=" + ctype)
    for h in _DIAG_HEADERS:
        val = resp.headers.get(h)
        if val:
            bits.append("%s=%s" % (h, val))
    return "; ".join(bits)


def _request_summary(req_kw):
    """把「本次实际发出的字段」压成一行，便于核对参数类型与取值。

    刻意用 repr 而非 str：这样 `n=1`（generations 的 JSON 整数）与 `n='1'`
    （edits 的 multipart 字符串，协议决定的，见 _form_value）在日志里一眼可辨。
    网关回「n 必须是整数」这类 400 时，这一行能直接确认我们究竟发了什么。
    密钥只在 Authorization 请求头里，不会出现在这里。
    """
    src = req_kw.get("json")
    if src is None:
        src = req_kw.get("data")
    parts = []
    if isinstance(src, dict):
        for k in sorted(src):
            v = src[k]
            if k == "prompt":
                text = _clean(v)
                v = text[:120] + ("…(共 %d 字)" % len(text) if len(text) > 120 else "")
            parts.append("%s=%r" % (k, v))
    # 只报 multipart 文件字段名与张数，不碰图片内容本身。
    counts = {}
    for item in req_kw.get("files") or []:
        if isinstance(item, (tuple, list)) and item:
            counts[item[0]] = counts.get(item[0], 0) + 1
    if counts:
        parts.append("files=" + "+".join("%s×%d" % kv for kv in sorted(counts.items())))
    return ", ".join(parts)


def _status_hint(code, message=""):
    """按状态码给一句「下一步查哪里」的提示；message 用于区分同码不同因。"""
    low = (message or "").lower()
    if code in (401, 403):
        return "鉴权失败：检查「密钥」是否正确/已过期，以及该密钥在此网关是否有图像权限。"
    if code == 404:
        return ("端点不存在：检查「接口地址」是否漏了/多了 `/v1`（本插件会自行拼接 "
                "`/images/generations` 与 `/images/edits`），以及该网关是否真的支持该端点。")
    if code == 400:
        # 审核拦截同样走 400，但与参数无关——若还让用户去核对参数，纯属误导。
        # 注意「generated images ... unsafe」指**产物**被判定违规(生成已发生)，
        # 与「输入图/提示词被拒」不同，重试或改参数都无用，只能改提示词/换图。
        if ("unsafe" in low or "safety" in low or "moderation" in low
                or "content_policy" in low or "content policy" in low):
            return ("这是**内容审核拦截**，不是参数问题——别去核对参数。"
                    "若报的是「generated images appear to be unsafe」，说明图已经生成、"
                    "但成品被判定违规而不返回（可能已计费）。重试通常无效："
                    "请改提示词、或更换/裁剪参考图中的敏感部分；"
                    "「审核级别」设为 low 也许可放宽（需网关支持）。")
        return ("参数被上游拒绝：对照上面「本次发送」逐项核对。注意 edits 走 "
                "multipart，协议上所有值都是字符串（如 n='1'）；若网关对某字段"
                "强校验类型，可把对应参数设为 default/0 使其整字段不发送。")
    if code == 413:
        return "请求体过大：减少参考图数量或先缩小参考图尺寸。"
    if code == 429:
        return "被限流：降低并发或加大「重试次数」；插件会优先按响应的 Retry-After 等待。"
    if 520 <= code <= 527:
        return ("520~527 是 Cloudflare 边缘码，表示**边缘到你的网关源站之间**出错"
                "（源站超时/崩溃/握手失败），不是本插件的参数问题。生图耗时长时尤其"
                "常见：可开「流式」保活、加大「重试次数」，或联系网关方。")
    if code in (500, 502, 503, 504):
        # 网关把上游响应解析失败也报 500，这类与本次参数无关，说清楚免得白折腾。
        if "looking for beginning of value" in low or "bad_response_body" in low:
            return ("网关**解析上游响应失败**（它期待 JSON 却收到别的内容，常见于上游返回"
                    "了 HTML 错误页或空响应）。属网关/上游侧故障，与本次参数无关，"
                    "重试或稍后再试；持续出现请联系网关方。")
        return "网关/上游暂时不可用：通常重试即可；持续出现请联系网关方。"
    return ""


def _exc_chain(exc, limit=12):
    """迭代异常链上的每一环，用于**结构化**判定内层原因（取代字符串匹配）。

    requests 把底层异常包了三层，且各层挂载位置不同（实测 requests 2.32 /
    urllib3 2.x，见下），只看 __cause__ 会漏：

      场景「隧道未建成」: requests.ConnectionError
                          -> args[0] = urllib3.ProtocolError
                          -> __context__ = http.client.RemoteDisconnected
      场景「隧道已建成」: requests.ProxyError
                          -> args[0] = urllib3.MaxRetryError
                          -> .reason  = urllib3.ProxyError
                          -> __cause__= http.client.RemoteDisconnected

    故依次尝试 __cause__ / __context__ / args[0] / MaxRetryError.reason。
    这样判定内层原因就能用 isinstance，而不是在 str(exc) 里找关键字——后者会随
    上游库措辞改动而静默失效。
    """
    seen = set()
    cur = exc
    depth = 0
    while cur is not None and id(cur) not in seen and depth < limit:
        seen.add(id(cur))
        depth += 1
        yield cur
        nxt = cur.__cause__ or cur.__context__
        if nxt is None and getattr(cur, "args", None):
            first = cur.args[0]
            if isinstance(first, BaseException):
                nxt = first
        if nxt is None:
            nxt = getattr(cur, "reason", None)
            if not isinstance(nxt, BaseException):
                nxt = None
        cur = nxt


def _chain_has(exc, *types):
    """异常链上是否出现过指定类型（结构化判定）。"""
    return any(isinstance(e, types) for e in _exc_chain(exc))


def _proxy_env_summary():
    """本进程实际生效的代理环境变量摘要；没有则返回 ""。

    requests 默认 trust_env=True，会自动读这些变量——ComfyUI 从 shell 继承到的
    代理会**静默**接管所有出网请求。排查连接类报错时这是首要嫌疑，必须显式报出来。
    """
    bits = []
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        val = os.environ.get(name)
        if val:
            bits.append("%s=%s" % (name, val))
            break
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy")
    if bits and no_proxy:
        bits.append("NO_PROXY=%s" % no_proxy)
    return "; ".join(bits)


def _transport_hint(exc, host=""):
    """网络层异常 -> 「这句报错到底什么意思、下一步查哪里」。

    判定**全部基于异常类型与异常链**（isinstance），不靠 str(exc) 里的关键字。

    纠正两处极易误读的措辞（均由对照实验判定，非推测）：

    1. **「Max retries exceeded」与本节点的「重试次数」无关**。那是 urllib3 自己的
       连接级重试计数（requests 默认 0 次），措辞固定。把「重试次数」设成 0 也照样
       出现这句，别据此以为插件在偷偷重试。判据：链上有 urllib3 MaxRetryError。

    2. **ProxyError("Unable to connect to proxy") 的字面意思是错的**。它读起来像
       「连不上代理、请求根本没发出去」，实际往往相反：CONNECT 隧道**早已建成、
       请求已完整送达上游**，是代理在**等上游响应时**把隧道掐断了。根因是 urllib3
       仅在 has_connected_to_proxy 为假时才套这层 ProxyError，而该标志在复用连接
       等路径上并不可靠，于是响应阶段的断连被贴上了连接阶段的标签。
       假代理对照实验（两种场景的**结构**可区分，故无需字符串匹配）：
         - 收到 CONNECT 即关闭(隧道未建成) -> requests.ConnectionError + ProtocolError
         - 回 200 建隧道、之后不回响应     -> requests.ProxyError + MaxRetryError
       这个区别很要紧：请求既已送达，上游就**可能已经生成、已经计费**，
       不能当成「没发出去的空跑」。
    """
    hints = []
    dropped = _chain_has(exc, http.client.RemoteDisconnected, ConnectionResetError)

    if _chain_has(exc, urllib3.exceptions.MaxRetryError):
        hints.append("「Max retries exceeded」是 urllib3 的固定措辞，指它自己的连接级"
                     "重试(默认 0 次)，**与本节点的「重试次数」无关**，设成 0 也会出现。")

    # 顺序要紧：ProxyError/SSLError/ConnectTimeout 都是 requests.ConnectionError 的
    # 子类，必须先判具体类型，最后才落到笼统的 ConnectionError。
    if isinstance(exc, requests.exceptions.ProxyError):
        if dropped:
            hints.append("尽管写着「Unable to connect to proxy」，这**不是连不上代理**："
                         "隧道已建成、请求已送达上游，是代理在等响应时掐断了连接。"
                         "因此**上游可能已经生成并计费**，请去网关后台核对用量，"
                         "不要当成空跑。")
        else:
            hints.append("代理层报错：先确认本机代理进程(Clash/V2Ray/公司代理等)正常工作。")
    elif isinstance(exc, requests.exceptions.SSLError):
        hints.append("TLS 握手失败：常见于代理做了中间人解密，或「接口地址」把 https "
                     "写成了 http(反之亦然)。")
    elif isinstance(exc, requests.exceptions.ConnectTimeout):
        hints.append("连接超时(%ds 内没连上)：多为网络/代理不通，与参数无关，"
                     "请求**未送达**。" % CONNECT_TIMEOUT)
    elif isinstance(exc, requests.exceptions.ReadTimeout):
        hints.append("读取超时：请求**已发出**、上游迟迟没回完。生图慢属正常，"
                     "可调大「超时秒数」。")
    elif isinstance(exc, requests.exceptions.ChunkedEncodingError):
        hints.append("响应传输中断(分块编码未收完)：请求已送达、上游回传中途断开，"
                     "**可能已计费**。")
    elif isinstance(exc, requests.exceptions.ConnectionError) and dropped:
        hints.append("连接被对端关闭：可能是代理/网关空闲超时或上游崩溃。")

    env = _proxy_env_summary()
    if env:
        hints.append("本进程走的是代理: %s。生图要几十秒到十几分钟，而多数代理的"
                     "空闲/单连接时限远小于此，容易中途掐断。可把网关域名%s加进 "
                     "NO_PROXY 直连，或调大代理自身的 timeout。"
                     % (env, ("(%s)" % host) if host else ""))
    elif isinstance(exc, requests.exceptions.ProxyError):
        hints.append("注意：报错来自代理层，但本进程环境变量里没看到代理设置——"
                     "可能是系统级代理或代理自动配置(PAC)在生效。")

    return hints


def _transport_error_message(label, exc, url=""):
    """网络层异常 -> 多行可排查消息（与 _http_error_message 同风格）。"""
    host = ""
    if url:
        m = re.match(r"https?://([^/:]+)", url)
        if m:
            host = m.group(1)
    lines = ["[GPT-Image] %s 请求失败: %s" % (label, exc),
             "  异常类型: %s" % exc.__class__.__name__]
    # 内层原因用类型名列出，比在长串消息里找关键字直观，也便于报障时贴给网关方。
    chain = [type(e).__name__ for e in _exc_chain(exc)][1:]
    if chain:
        lines.append("  内层原因链: " + " <- ".join(chain))
    for hint in _transport_hint(exc, host):
        lines.append("  提示: " + hint)
    return "\n".join(lines)


def _safe_body(resp):
    try:
        return resp.text or ""
    except Exception as exc:                      # 正文读取本身也可能失败
        return "<读取响应正文失败: %s>" % exc


def _http_error_message(label, resp, req_kw=None):
    """构造多行、可排查的错误信息；同时把完整正文打进 ComfyUI 日志。"""
    body = _safe_body(resp)
    short = _extract_error_message(body, resp.headers.get("Content-Type", ""))
    reason = _clean(getattr(resp, "reason", "") or "")
    lines = ["[GPT-Image] %s 失败 (%s%s): %s"
             % (label, resp.status_code, " " + reason if reason else "", short)]
    for tag, text in (("诊断", _response_diag(resp)),
                      ("本次发送", _request_summary(req_kw or {})),
                      ("提示", _status_hint(resp.status_code, short))):
        if text:
            lines.append("  %s: %s" % (tag, text))
    # 摘要之外的内容不丢：完整正文进日志，异常框只留提炼后的摘要。
    if _collapse(body) != short:
        print("[GPT-Image] %s 上游完整响应正文 (%d 字节)：\n%s"
              % (label, len(body), body))
    return "\n".join(lines)


def _post_with_retry(url, *, headers, timeout, stream, attempts, label, **req_kw):
    """POST + 对瞬时错误(429/5xx/超时/连接重置)指数退避重试。

    只覆盖「请求建立 + 首个状态码」阶段；流式响应一旦在调用方开始迭代就不再重试，
    避免半消费的流被重放。参考图以 bytes 传入(非文件句柄)，可安全跨重试重发。
    返回 status==200 的 response，否则 raise RuntimeError。

    抛出的消息里**必须带上每次尝试的历史**：只报最后一次的话，会出现「控制台看到
    的原因」与「节点弹窗里的原因」对不上——比如第 1 次 502、第 2 次才是真正的 400
    参数错误，或反过来第 1 次就是 400、后面被网关掩成 502。历史行让两者一致可核对。
    """
    attempts = max(1, int(attempts))
    last_err = None
    history = []            # [(第几次, 状态码或异常名, 提炼后的原因)]

    def _with_history(msg):
        if len(history) <= 1:
            return msg
        lines = [msg, "  尝试历史(共 %d 次，最后一次即上面的报错):" % len(history)]
        for i, what, why in history:
            lines.append("    第 %d 次: %s — %s" % (i, what, why[:160]))
        lines.append("  说明: 「重试次数」只对 429/5xx/超时/连接重置这类瞬时错误生效；"
                     "若历史里出现过 4xx(参数/审核问题)，那次才是根因，重试无用。")
        return "\n".join(lines)

    for attempt in range(1, attempts + 1):
        try:
            r = _interruptible_post(url, headers=headers,
                                    timeout=timeout, stream=stream, **req_kw)
        except requests.RequestException as e:
            last_err = e
            history.append((attempt, e.__class__.__name__, str(e)))
            if attempt >= attempts:
                raise RuntimeError(_with_history(
                    _transport_error_message(label, e, url)))
            wait = _backoff_seconds(attempt)
            print("[GPT-Image] %s 第 %d/%d 次请求异常(%s)，%.1fs 后重试。"
                  % (label, attempt, attempts, e.__class__.__name__, wait))
            time.sleep(wait)
            continue

        if r.status_code == 200:
            return r

        why = _extract_error_message(_safe_body(r), r.headers.get("Content-Type", ""))
        history.append((attempt, "HTTP %s" % r.status_code, why))

        # 非 200：可重试状态码且还有次数 -> 退避重试(优先用服务端的 Retry-After)。
        if attempt < attempts and r.status_code in RETRYABLE_STATUS:
            wait = _retry_after_seconds(r)
            if wait is None:
                wait = _backoff_seconds(attempt)
            wait = min(MAX_BACKOFF, wait)
            # 日志里带上提炼后的原因，避免「重试了但不知道在重试什么」。
            print("[GPT-Image] %s 第 %d/%d 次返回 %s，%.1fs 后重试。原因: %s"
                  % (label, attempt, attempts, r.status_code, wait, why[:200]))
            r.close()
            time.sleep(wait)
            continue

        msg = _http_error_message(label, r, req_kw)
        r.close()
        raise RuntimeError(_with_history(msg))

    raise RuntimeError(_with_history(
        _transport_error_message(label, last_err, url)))


def generate_images(base_url, api_key, params, timeout=900, stream=False,
                    partial_images=1, attempts=1):
    """POST {base}/images/generations (JSON)。返回 IMAGE tensor [N,H,W,3]。"""
    base = _normalize_base_url(base_url)
    headers = _auth_headers(api_key)
    headers["Content-Type"] = "application/json"
    payload = dict(params)
    if stream:
        payload["stream"] = True
        payload["partial_images"] = _coerce_int(partial_images) or 0
    r = _post_with_retry(base + "/images/generations", headers=headers, timeout=timeout,
                         stream=stream, attempts=attempts, label="generations", json=payload)
    return _stack(_result_tensors(r, timeout, stream, attempts))


def edit_images(base_url, api_key, params, ref_pngs, mask_png=None,
                timeout=900, stream=False, partial_images=1, attempts=1):
    """POST {base}/images/edits (multipart, image[])。返回 IMAGE tensor [N,H,W,3]。"""
    if not ref_pngs:
        raise ValueError("[GPT-Image] 编辑端点(/images/edits) 至少需要一张参考图，请连接「图片1」。")
    base = _normalize_base_url(base_url)
    headers = _auth_headers(api_key)  # 不设 Content-Type，交给 requests 生成 multipart 边界
    # multipart 字段值在协议层只能是字符串，故逐个走 _form_value 给出规范写法
    # （int 不带小数点、bool 用小写 true/false），不依赖 python 的 str()。
    form = {k: _form_value(v) for k, v in params.items() if v is not None}
    if stream:
        form["stream"] = "true"
        form["partial_images"] = _form_value(_coerce_int(partial_images) or 0)
    # 多参考图通过重复的 image[] 字段传递 (OpenAI Images API 规范)。
    files = [("image[]", ("ref%d.png" % i, png, "image/png"))
             for i, png in enumerate(ref_pngs)]
    if mask_png is not None:
        files.append(("mask", ("mask.png", mask_png, "image/png")))
    r = _post_with_retry(base + "/images/edits", headers=headers, timeout=timeout,
                         stream=stream, attempts=attempts, label="edits", data=form, files=files)
    return _stack(_result_tensors(r, timeout, stream, attempts))


def _stack(tensors):
    """把多张 [1,H,W,3] 合并成一个 [N,H,W,3] 批次；尺寸不一致则只返回第一张。

    ComfyUI 的 IMAGE 批次要求同尺寸，故尺寸不一致时无法合并。这里把「丢了几张」
    说清楚(而不是静默只给一张)——否则用户填了 数量=2 却只拿到 1 张，完全无从判断
    是网关只给了一张、还是被这里丢掉了。
    """
    if not tensors:
        raise RuntimeError("[GPT-Image] 没有解析出任何图片。")
    if len(tensors) == 1:
        return tensors[0]
    if len({t.shape for t in tensors}) == 1:
        return torch.cat(tensors, dim=0)
    shapes = ", ".join(str(tuple(t.shape[1:])) for t in tensors)
    print("[GPT-Image] 上游返回了 %d 张但尺寸不一致(%s)，ComfyUI 批次要求同尺寸，"
          "仅输出第一张。可把「宽/高」设为固定值以避免。" % (len(tensors), shapes))
    return tensors[0]


def collect_ref_pngs(image_batches):
    """list of ComfyUI IMAGE tensors ([B,H,W,C]) 或 None -> 按顺序的 PNG 字节列表。"""
    pngs = []
    for batch in image_batches or []:
        if batch is None:
            continue
        for i in range(batch.shape[0]):
            pngs.append(tensor_to_png_bytes(batch[i]))
    return pngs
