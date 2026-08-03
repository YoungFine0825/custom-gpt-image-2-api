# -*- coding: utf-8 -*-
"""无网络单测 (no-network unit test)：验证参数×模型联动校验与重试判定逻辑。

只测纯函数，不发任何请求。用 ComfyUI 的 python 运行：
    python test_validation.py
全部通过打印 "ALL PASS"，任何断言失败会抛出。
"""

import api_client as ac


def expect_ok(model, **kw):
    """build_params 应成功返回 dict。"""
    p = ac.build_params(model=model, prompt="x", **kw)
    assert isinstance(p, dict), p
    return p


def expect_err(model, needle=None, **kw):
    """build_params 应 raise ValueError；needle 若给出则须出现在消息里。"""
    try:
        ac.build_params(model=model, prompt="x", **kw)
    except ValueError as e:
        if needle is not None:
            assert needle in str(e), "期望消息含 %r，实际: %s" % (needle, e)
        return
    raise AssertionError("期望 %s 报错但通过了 (kw=%s)" % (model, kw))


def main():
    # ── gpt-image-2 尺寸：合法 ──
    expect_ok("gpt-image-2", size="1024x1024")
    expect_ok("gpt-image-2", size="1536x1024")
    expect_ok("gpt-image-2", size="auto")
    expect_ok("gpt-image-2")  # 未给 size -> auto，不校验

    # ── gpt-image-2 尺寸：非法 ──
    expect_err("gpt-image-2", "16 的倍数", size="1000x1000")      # 非 16 倍数
    expect_err("gpt-image-2", "最长边", size="4096x1024")          # 超最大边(且是16倍数)
    expect_err("gpt-image-2", "3:1", size="3072x512")             # 比例 6:1 超 3:1
    expect_err("gpt-image-2", "总像素", size="512x512")            # 262144 < 655360 下限

    # ── legacy 模型尺寸白名单 ──
    expect_ok("gpt-image-1.5", size="1024x1024")
    expect_ok("gpt-image-1.5", size="auto")
    expect_err("gpt-image-1.5", "只支持", size="2048x2048")

    # ── 未知模型(自定义网关)：一律软放行，不报错 ──
    expect_ok("my-gateway-model", size="1000x1000")   # 非 16 倍数也只警告
    expect_ok("my-gateway-model", size="4096x4096")   # 超 gpt-image-2 限制也放行

    # ── 透明背景联动 ──
    expect_err("gpt-image-2", "不支持透明", background="transparent", output_format="png")
    expect_err("gpt-image-1.5", "jpeg", background="transparent", output_format="jpeg")
    expect_ok("gpt-image-1.5", background="transparent", output_format="png")
    # 未知模型 + transparent + png：放行；+ jpeg：物理不可能，仍拦截
    expect_ok("my-gateway-model", background="transparent", output_format="png")
    expect_err("my-gateway-model", "jpeg", background="transparent", output_format="jpeg")

    # ── input_fidelity 联动 ──
    # gpt-image-2 不支持 -> 被丢弃(不报错)
    p = expect_ok("gpt-image-2", size="1024x1024", input_fidelity="high")
    assert "input_fidelity" not in p, p
    # gpt-image-1.5 支持 -> 保留
    p = expect_ok("gpt-image-1.5", size="1024x1024", input_fidelity="high")
    assert p.get("input_fidelity") == "high", p
    # default -> 本就不发送
    p = expect_ok("gpt-image-1.5", size="1024x1024", input_fidelity="default")
    assert "input_fidelity" not in p, p

    # ── output_compression 仅 jpeg/webp 生效 ──
    p = expect_ok("gpt-image-1.5", size="1024x1024", output_format="png", output_compression=50)
    assert "output_compression" not in p, p
    p = expect_ok("gpt-image-1.5", size="1024x1024", output_format="jpeg", output_compression=50)
    assert p.get("output_compression") == 50, p

    # ── 重试相关纯函数 ──
    assert ac._backoff_seconds(1) == 2.0
    assert ac._backoff_seconds(10) == ac.MAX_BACKOFF   # 封顶

    class _Resp:
        def __init__(self, ra):
            self.headers = {"Retry-After": ra} if ra is not None else {}

    assert ac._retry_after_seconds(_Resp("12")) == 12.0
    assert ac._retry_after_seconds(_Resp(None)) is None
    assert ac._retry_after_seconds(_Resp("Wed, 21 Oct 2099 07:28:00 GMT")) is None  # HTTP-date 不解析
    assert 429 in ac.RETRYABLE_STATUS and 400 not in ac.RETRYABLE_STATUS
    # Cloudflare 边缘 5xx(520~527) 属瞬时故障，应重试
    assert 520 in ac.RETRYABLE_STATUS and 524 in ac.RETRYABLE_STATUS

    # ── n(数量) 参数：>0 才发送，<=0 / 非法 -> 整字段不发 ──
    assert expect_ok("gpt-image-2", n=1).get("n") == 1
    assert expect_ok("gpt-image-2", n=4).get("n") == 4
    assert "n" not in expect_ok("gpt-image-2", n=0)          # 0 = 不发送(逃生口)
    assert "n" not in expect_ok("gpt-image-2", n=-1)
    assert "n" not in expect_ok("gpt-image-2", n="abc")       # 解析不了 -> 不发送
    assert expect_ok("gpt-image-2", n="3").get("n") == 3      # 字符串数字收口成 int
    assert expect_ok("gpt-image-2", n=2.0).get("n") == 2      # float -> int
    # 发送的必须是真 int，否则 JSON body 里会变成 "3"/3.0
    assert isinstance(expect_ok("gpt-image-2", n=2.0)["n"], int)

    # ── _coerce_int ──
    assert ac._coerce_int(3) == 3
    assert ac._coerce_int("3") == 3
    assert ac._coerce_int(3.0) == 3
    assert ac._coerce_int(None) is None
    assert ac._coerce_int(True) is None      # bool 不当数字用
    assert ac._coerce_int("x") is None

    # ── _form_value：multipart 字段值的规范写法（协议上只能是字符串）──
    assert ac._form_value(1) == "1"
    assert ac._form_value(1.0) == "1"        # 不能是 "1.0"，严格网关会判成非整数
    assert ac._form_value(True) == "true"    # 不能是 "True"
    assert ac._form_value(False) == "false"
    assert ac._form_value("auto") == "auto"

    # ── 错误正文提炼（修复「上游报错被截断到 500 字、看不到原因」）──
    # OpenAI 风格 JSON：挖出 error.message，并带上 code/type
    msg = ac._extract_error_message(
        '{"error":{"code":"INVALID_PARAM","message":"n 必须是 1 到 128 的整数",'
        '"type":"invalid_request_error"}}', "application/json")
    assert "n 必须是 1 到 128 的整数" in msg and "INVALID_PARAM" in msg
    # 兼容网关的其它形状
    assert "boom" in ac._extract_error_message('{"message":"boom"}', "application/json")
    assert "bad" in ac._extract_error_message('{"error":"bad"}', "application/json")
    assert "d1" in ac._extract_error_message('{"detail":"d1"}', "application/json")
    # Cloudflare HTML 错误页：关键信息在 <title> 里，旧实现会被前 500 字的
    # DOCTYPE/条件注释/meta 挤掉，这里必须能提出来。
    html = ("<!DOCTYPE html>\n<!--[if lt IE 7]> <html class=\"no-js ie6\"> <![endif]-->\n"
            + "<!-- %s -->\n" % ("x" * 600)
            + "<head>\n<title>eaheng.com | 520: Web server is returning an unknown error"
              "</title>\n<meta charset=\"UTF-8\" />\n<style>.a{color:red}</style>\n</head>"
              "<body><h1>Error 520</h1></body></html>")
    got = ac._extract_error_message(html, "text/html; charset=UTF-8")
    assert "520" in got and "unknown error" in got, got
    assert "DOCTYPE" not in got and "<meta" not in got, got   # 标签/样板被剔除
    assert "color:red" not in got, got                       # <style> 内容不进摘要
    assert len(got) <= ac.MAX_ERROR_BODY
    # 空正文与纯文本
    assert "空正文" in ac._extract_error_message("", "text/plain")
    assert ac._extract_error_message("  plain  boom \n", "text/plain") == "plain boom"

    # ── 请求字段摘要：用 repr 区分 n=1(JSON int) 与 n='1'(multipart str) ──
    assert "n=1" in ac._request_summary({"json": {"n": 1, "prompt": "p"}})
    assert "n='1'" in ac._request_summary({"data": {"n": "1", "prompt": "p"}})
    long_prompt = "字" * 300
    summary = ac._request_summary({"json": {"prompt": long_prompt}})
    assert "共 300 字" in summary and len(summary) < 300   # 长提示词被截断
    assert ac._request_summary({}) == ""
    # multipart 只报文件字段名与张数（不碰图片内容）
    files = [("image[]", ("a.png", b"x", "image/png")),
             ("image[]", ("b.png", b"y", "image/png")),
             ("mask", ("m.png", b"z", "image/png"))]
    fs = ac._request_summary({"data": {"n": "1"}, "files": files})
    assert "image[]×2" in fs and "mask×1" in fs, fs

    # ── 状态码提示 ──
    assert ac._status_hint(520) and "Cloudflare" in ac._status_hint(520)
    assert ac._status_hint(524) and ac._status_hint(401) and ac._status_hint(400)
    assert ac._status_hint(200) == ""

    # ── 错误信息组装：状态码 + 摘要 + 诊断头 + 本次发送 + 提示 ──
    class _HttpResp:
        status_code = 520
        reason = "Origin Error"
        url = "https://gw.example.com/v1/images/edits?trace=1"
        headers = {"Content-Type": "text/plain", "cf-ray": "8ab-HKG",
                   "server": "cloudflare"}
        text = "boom"

    built = ac._http_error_message("edits", _HttpResp(), {"data": {"n": "1"}})
    assert "(520 Origin Error)" in built and "boom" in built
    assert "cf-ray=8ab-HKG" in built                 # 报障用的追踪头
    assert "trace=1" not in built                    # query 被剥掉，不外泄
    assert "n='1'" in built                          # 实际发出的值与类型
    assert "Cloudflare" in built                     # 状态码提示

    # ── 端到端：edits 的 multipart 表单值都是「规范字符串」，n 可整字段不发 ──
    orig_post = ac._post_with_retry
    sent = {}

    def _fake_post(url, *, headers, timeout, stream, attempts, label, **req_kw):
        sent.clear()
        sent.update(req_kw)
        raise RuntimeError("stop-before-response")   # 只看发出去什么，不解析响应

    ac._post_with_retry = _fake_post
    try:
        for kwargs, check in (
            ({"n": 1}, lambda f: f.get("n") == "1"),
            ({"n": 0}, lambda f: "n" not in f),      # 0 = 不发送
        ):
            p = ac.build_params(model="gpt-image-2", prompt="x", size="1024x1024", **kwargs)
            try:
                ac.edit_images("https://gw.example.com/v1", "sk-test", p, [b"fake-png"],
                               stream=True, partial_images=2)
            except RuntimeError as e:
                assert "stop-before-response" in str(e), e
            form = sent.get("data") or {}
            assert check(form), form
            assert all(isinstance(v, str) for v in form.values()), form
            assert form.get("stream") == "true"          # 不是 "True"
            assert form.get("partial_images") == "2"
            assert form.get("model") == "gpt-image-2"
    finally:
        ac._post_with_retry = orig_post

    # ── size_from_wh：0 -> auto，>0 -> 拼接（不再自行打印/校验）──
    assert ac.size_from_wh(0, 0) == "auto"
    assert ac.size_from_wh(1024, 1536) == "1024x1536"

    # ── snap_dim：圆整到 step 倍数 + clamp 到 [lo,hi]（尺寸规范化节点用）──
    assert ac.snap_dim(1000, 16, 16, 3840) == 1008      # 最近 16 倍数
    assert ac.snap_dim(1020, 16, 16, 3840) == 1024
    assert ac.snap_dim(24, 16, 16, 3840) == 32          # 逢中向上
    assert ac.snap_dim(5, 16, 16, 3840) == 16           # 圆整得 0，被 lo 抬到 16
    assert ac.snap_dim(5000, 16, 16, 3840) == 3840      # 被 hi 压回
    assert ac.snap_dim(0, 16, 20, 3840) == 32           # lo=20 向上对齐到 32
    assert ac.snap_dim(9999, 16, 16, 3850) == 3840      # hi=3850 向下对齐到 3840
    assert ac.snap_dim(9999) == ac.GPT_IMAGE_2_MAX_EDGE  # hi 缺省=3840
    assert ac.snap_dim(1000, 8) == 1000                 # step=8：1000 已是 8 倍数
    assert ac.snap_dim(1000, 32, 16, 3840) == 992       # step=32：(1000+16)//32*32

    print("ALL PASS")


if __name__ == "__main__":
    main()
