# -*- coding: utf-8 -*-
"""配置节点 (config node)：让用户自定义 base_url 与 api_key。

输出自定义类型 IMAGE_API_CONFIG (一个 (base_url, api_key) 元组)，
连接到生成节点的「配置」输入，即可「设置一次，多个节点复用」。

安全提示 (security note)：本插件通过前端扩展 (web/gpt_image_config_security.js)
把「密钥」widget 的 serialize 关闭，使 api_key 不会写进保存/导出的工作流 .json
（也不进导出 PNG 内嵌的 workflow），从根源避免分享工作流时泄露密钥；
同时把密钥按 base_url 存进浏览器 localStorage，本机重开工作流时自动回填、无需重填。
"""


from . import api_client


class ImageAPIConfig:
    """GPT-Image API 配置：自定义 base_url + api_key。"""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                # 无任何预设值，完全由用户填写自己的 API 地址。
                "接口地址": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "https://your-endpoint.example.com/v1",
                }),
                "密钥": ("STRING", {
                    "default": "",
                    "multiline": False,
                    "placeholder": "sk-...",
                }),
            },
        }

    RETURN_TYPES = ("IMAGE_API_CONFIG",)
    RETURN_NAMES = ("配置",)
    FUNCTION = "build"
    CATEGORY = "GPT-Image"

    def build(self, **kw):
        # 校验逻辑在 api_client.validate_credentials（与本地配置文件共用，
        # 保证「配置节点连线」与「免连线配置文件」两条路行为一致）。
        base_url, api_key = api_client.validate_credentials(
            kw.get("接口地址"), kw.get("密钥"))
        return ((base_url, api_key),)
