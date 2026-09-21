"""按"接管对象"拆分的功能模块。

- ``requirements``：模块前置条件（需要宿主关闭哪些能力才可用）与可用性判定；
- ``post_process_takeover``：回复后处理接管（错别字 + 分段 + 多段发送）；
- ``quote_takeover``：引用回复接管（直接 / 引用 / @ / 引用＋@ 的权重抽取）。

两个接管模块都以 **mixin** 形式提供能力，由 ``plugin.py`` 的插件类组合（SDK 用
``dir(instance)`` 收集组件，继承来的 HookHandler 一样会被注册）。共享基础设施
（回复轮记录、文本规则、表情包相关功能）仍留在 ``plugin.py``，mixin 通过 ``self`` 访问。
"""
