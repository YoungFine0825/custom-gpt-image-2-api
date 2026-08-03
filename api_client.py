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
import io
import json
import re
import socket
import threading
import time

import numpy as np
import requests
import torch
from PIL import Image
from urllib3.connection import HTTPConnection

# 可选枚举参数的合法取值（第一项 default = 不发送）。
QUALITY_OPTIONS = ["default", "auto", "high", "medium", "low"]
BACKGROUND_OPTIONS = ["default", "auto", "transparent", "opaque"]
OUTPUT_FORMAT_OPTIONS = ["default", "png", "jpeg", "webp"]
MODERATION_OPTIONS = ["default", "auto", "low"]
# 输入保真度 (input_fidelity)：仅 /images/edits 有意义；default = 不发送。
INPUT_FIDELITY_OPTIONS = ["default", "low", "high"]

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

    return params


def build_params(model, prompt, size="auto", n=1, quality="default",
                 background="default", output_format="default",
                 output_compression=None, moderation="default",
                 input_fidelity="default"):
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


def _images_from_response(resp_json, timeout):
    """OpenAI ImagesResponse -> list of ComfyUI IMAGE tensors [1,H,W,3]。

    优先读内联的 base64 (b64_json，GPT image 模型默认返回)；否则回退到 url
    (DALL-E 风格)。url 由服务端返回，只有服务端确实返回时才会去取。
    """
    data = (resp_json or {}).get("data") or []
    if not data:
        # 200 但没有图：可能是网关把错误塞进了 200 响应，故沿用同一套提炼逻辑。
        detail = _dig_error_message(resp_json) or _collapse(str(resp_json))[:MAX_ERROR_BODY]
        raise RuntimeError("[GPT-Image] 响应里没有图片数据 (data 为空): %s" % detail)
    tensors = []
    for item in data:
        b64 = item.get("b64_json")
        if b64:
            tensors.append(bytes_to_tensor(base64.b64decode(b64)))
            continue
        url = item.get("url")
        if url:
            try:
                r = _session().get(url, timeout=(CONNECT_TIMEOUT, timeout))
            except requests.RequestException as e:
                raise RuntimeError("[GPT-Image] 下载结果图片失败: %s" % e)
            if r.status_code != 200:
                raise RuntimeError("[GPT-Image] 下载结果图片失败 (%s)" % r.status_code)
            tensors.append(bytes_to_tensor(r.content))
            continue
        raise RuntimeError("[GPT-Image] 结果项既无 b64_json 也无 url: %s" % str(item)[:300])
    return tensors


def _images_from_stream(resp):
    """解析 SSE 流：累积 partial 预览，返回最终 completed 的图 [1,H,W,3]。

    事件形如 (data: 后是 JSON，带 type 字段)：
      image_generation.partial_image / image_edit.partial_image  -> 进度预览
      image_generation.completed     / image_edit.completed      -> 最终图
    """
    final_b64 = None
    last_partial = None
    for raw in resp.iter_lines(decode_unicode=True):
        _check_interrupt()   # SSE 流式迭代间隙检查中断，比单次阻塞更及时
        if not raw:
            continue
        line = raw.strip()
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        b64 = obj.get("b64_json")
        if not b64:
            continue
        etype = str(obj.get("type", ""))
        if etype.endswith("completed"):
            final_b64 = b64
        elif etype.endswith("partial_image"):
            last_partial = b64
            print("[GPT-Image] 流式预览 #%s 已接收（保活中）" % obj.get("partial_image_index"))
        else:
            final_b64 = final_b64 or b64
    b64 = final_b64 or last_partial
    if not b64:
        raise RuntimeError("[GPT-Image] 流式响应里没有拿到图片（无 completed/partial 事件）。")
    return [bytes_to_tensor(base64.b64decode(b64))]


def _result_tensors(resp, timeout, stream):
    """按响应类型解析：SSE 走流式解析；否则(含网关未按 SSE 返回)走普通 JSON。"""
    if stream and "text/event-stream" in resp.headers.get("Content-Type", ""):
        return _images_from_stream(resp)
    return _images_from_response(resp.json(), timeout)


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


def _status_hint(code):
    """按状态码给一句「下一步查哪里」的提示。"""
    if code in (401, 403):
        return "鉴权失败：检查「密钥」是否正确/已过期，以及该密钥在此网关是否有图像权限。"
    if code == 404:
        return ("端点不存在：检查「接口地址」是否漏了/多了 `/v1`（本插件会自行拼接 "
                "`/images/generations` 与 `/images/edits`），以及该网关是否真的支持该端点。")
    if code == 400:
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
        return "网关/上游暂时不可用：通常重试即可；持续出现请联系网关方。"
    return ""


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
                      ("提示", _status_hint(resp.status_code))):
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
    """
    attempts = max(1, int(attempts))
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            r = _interruptible_post(url, headers=headers,
                                    timeout=timeout, stream=stream, **req_kw)
        except requests.RequestException as e:
            last_err = e
            if attempt >= attempts:
                raise RuntimeError("[GPT-Image] %s 请求失败: %s" % (label, e))
            wait = _backoff_seconds(attempt)
            print("[GPT-Image] %s 第 %d/%d 次请求异常(%s)，%.1fs 后重试。"
                  % (label, attempt, attempts, e.__class__.__name__, wait))
            time.sleep(wait)
            continue

        if r.status_code == 200:
            return r

        # 非 200：可重试状态码且还有次数 -> 退避重试(优先用服务端的 Retry-After)。
        if attempt < attempts and r.status_code in RETRYABLE_STATUS:
            wait = _retry_after_seconds(r)
            if wait is None:
                wait = _backoff_seconds(attempt)
            wait = min(MAX_BACKOFF, wait)
            # 日志里带上提炼后的原因，避免「重试了但不知道在重试什么」。
            why = _extract_error_message(_safe_body(r), r.headers.get("Content-Type", ""))
            print("[GPT-Image] %s 第 %d/%d 次返回 %s，%.1fs 后重试。原因: %s"
                  % (label, attempt, attempts, r.status_code, wait, why[:200]))
            r.close()
            time.sleep(wait)
            continue

        msg = _http_error_message(label, r, req_kw)
        r.close()
        raise RuntimeError(msg)

    raise RuntimeError("[GPT-Image] %s 请求失败: %s" % (label, last_err))


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
    return _stack(_result_tensors(r, timeout, stream))


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
    return _stack(_result_tensors(r, timeout, stream))


def _stack(tensors):
    """把多张 [1,H,W,3] 合并成一个 [N,H,W,3] 批次；尺寸不一致则只返回第一张。"""
    if len(tensors) == 1:
        return tensors[0]
    if len({t.shape for t in tensors}) == 1:
        return torch.cat(tensors, dim=0)
    print("[GPT-Image] 返回了多张不同尺寸的图片，无法合并为一个批次，仅输出第一张。")
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
