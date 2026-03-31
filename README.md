# astrbot_plugin_Video

一个基于 OpenAI 兼容 `chat/completions` 接口的视频生成 AstrBot 插件。

当前版本采用 **LLM Tool 调用模式**，不再提供普通用户手动输入命令触发的入口。  
也就是说，这个插件现在的定位是：

- 由 AstrBot 的 LLM 在对话中决定是否调用
- 插件负责异步提交视频任务
- 插件在任务完成后主动把结果回传到原会话

## 当前支持的能力

插件统一使用一个模型配置项，并支持 **视频预设**、**黑白名单**、**次数限制**、**签到**、**统计**、**上下文能力** 与 **冷却时间**，通过 `_conf_schema.json` 让用户自行配置：

- `base_url`
- `api_key`
- `model`
- `prompt_list`
- `user_blacklist`
- `group_blacklist`
- `user_whitelist`
- `group_whitelist`
- `enable_user_limit`
- `default_user_limit`
- `enable_group_limit`
- `default_group_limit`
- `enable_checkin`
- `checkin_add_count`
- `enable_context`
- `context_max_messages`
- `context_rounds`
- `enable_cooldown`
- `cooldown_seconds`

插件会将请求发送到：

- `{base_url}/v1/chat/completions`

并自动带上：

- `Authorization: Bearer {api_key}`

## 当前提供的 LLM Tool

### 1. `generate_text_video`
用于文生视频。

适用场景：
- 用户明确要求“根据一段文字生成视频”
- 用户没有提供图片
- 如果用户带了图片，不应调用这个工具

参数：
- `prompt(string)`: 视频描述文本

说明：
- 支持直接写完整提示词
- 也支持使用预设触发词，插件会自动替换成预设内容

---

### 2. `generate_first_last_video`
用于首尾帧生成视频。

适用场景：
- 用户明确要求“从第一张图过渡到第二张图”
- 用户消息中必须正好有 2 张图片
- 插件会按消息中的图片出现顺序识别首帧和尾帧
- 如果是回复消息中的图片，也会优先按回复链中的顺序提取
- 如果回复链为空，会尝试主动读取被引用消息中的图片
- 也支持从文本中识别图片链接、data URL、base64:// 图片来源
- 如果检测到 1 张、3 张或更多，工具会直接返回错误提示，不会自动生成

参数：
- `prompt(string)`: 过渡描述文本

说明：
- 支持直接写完整提示词
- 也支持使用预设触发词，插件会自动替换成预设内容

---

### 3. `generate_multi_image_video`
用于多图生成视频。

适用场景：
- 用户明确要求基于多张图片生成视频
- 至少要有 2 张图片
- 最大图片数量由配置 `max_images` 控制
- 插件会尽量保持图片在消息链和引用链中的原始顺序
- 也支持从消息文本、引用文本中识别图片链接、data URL、base64:// 图片来源

参数：
- `prompt(string)`: 视频描述文本

说明：
- 支持直接写完整提示词
- 也支持使用预设触发词，插件会自动替换成预设内容

---

## 调用方式说明

当前版本不再提供：

- `/文生视频`
- `/首尾帧生成视频`
- `/多图生成视频`

这类显式命令。

而是由 AstrBot 的 LLM 在对话中根据用户意图决定是否调用：

- `generate_text_video`
- `generate_first_last_video`
- `generate_multi_image_video`

工具内部会：

1. 校验冷却时间、黑名单、白名单、次数限制
2. 解析视频预设并组合最终提示词
3. 自动拼接最近几轮上下文内容（如果开启上下文能力）
4. 按消息链顺序提取图片
5. 如果存在回复图片，则按回复链顺序继续提取
6. 如果回复链为空，会尝试主动读取被引用消息
7. 兼容识别图片组件、文本图片链接、data URL、base64:// 来源
8. 创建异步任务
9. 立即发送“任务已提交”
10. 后台继续处理
11. 生成完成后主动通知用户

## 配置项

### base_url
OpenAI 兼容接口地址，例如：

```text
http://localhost:8000
```

### api_key
接口鉴权密钥。

### model
统一使用的视频模型名。

### timeout
请求超时时间，单位秒。

### max_images
多图生成时允许的最大图片数。

### download_video
是否下载视频到本地后再发送。当前版本预留配置，后续会继续完善本地下载发送能力。

### send_video_as_url
是否优先发送视频链接。

### prompt_list
视频预设列表。  
格式为：

```text
触发词:完整提示词
```

例如：

```text
电影感: cinematic camera movement, dramatic lighting, film look
竖屏推镜: smooth camera push-in, vertical composition, soft motion
```

当用户输入中包含这些触发词时，插件会自动替换为预设内容，并把其余文本作为补充要求拼接进去。

### user_blacklist / group_blacklist
用户黑名单和群组黑名单。  
命中后将无法使用视频功能。

### user_whitelist / group_whitelist
用户白名单和群组白名单。  
如果配置了白名单，则只有白名单中的对象可以使用视频功能。管理员默认不受限制。

### enable_user_limit / default_user_limit
是否启用用户次数限制，以及新用户的默认次数。

### enable_group_limit / default_group_limit
是否启用群组次数限制，以及新群组的默认次数。

### enable_checkin / checkin_add_count
是否启用签到功能，以及每次签到增加的视频次数。

### enable_context / context_max_messages / context_rounds
是否启用上下文能力、每个会话最多保留多少条消息，以及生成时最多引用多少条最近上下文。  
开启后，插件会把最近几轮对话内容拼接进最终视频提示词，尽量保持上下文一致性。

### enable_cooldown / cooldown_seconds
是否启用冷却时间，以及同一用户两次提交视频任务之间的最小间隔秒数。  
开启后，用户在冷却期间再次调用会收到冷却提示。

## 异步处理说明

所有任务都采用后台异步处理：

1. LLM 调用工具
2. 插件校验黑名单、次数限制、参数和图片数量
3. 插件解析预设并生成最终提示词
4. 插件立即返回“任务已提交”
5. 后台异步调用视频接口
6. 成功后主动往原会话回传结果
7. 失败后主动回传错误原因

任务记录会保存到 AstrBot 数据目录下：

```text
data/plugin_data/astrbot_plugin_Video/tasks/
```

## 返回格式兼容

由于不同 OpenAI 兼容服务的返回结构可能不同，当前版本会尽量兼容多种格式，例如：

- `video_url`
- `url`
- `result_url`
- `output_url`
- `file_url`
- `choices[].message.content`
- `choices[].delta.content`
- SSE `data:` 流式返回

如果返回内容中包含可识别的视频链接，插件会尽量提取并发送。

## 当前限制

当前版本先专注于稳定的基础能力，因此还未实现：

- 更复杂的自然语言意图识别
- 自动模式推断
- 多轮补图会话
- 任务查询工具
- 更细粒度的管理员面板与统计面板
- 多模型切换
- 本地视频下载发送的完整链路
- 更细粒度的视频结果组件发送适配

## 依赖安装

插件依赖：

```text
httpx>=0.27.0
```

## 开发说明

当前代码采用分层结构，避免把所有逻辑堆在 `main.py`：

- `main.py`：AstrBot 插件入口、LLM Tool、预设管理、次数管理、签到、统计、上下文与冷却逻辑
- `models.py`：数据模型
- `exceptions.py`：异常定义
- `openai_video_client.py`：OpenAI 兼容接口客户端
- `media_service.py`：图片提取与 base64 转换
- `task_repo.py`：任务持久化
- `task_service.py`：任务编排
- `message_service.py`：消息文案和通知
- `usage_repo.py`：次数、签到、统计存储
- `context_repo.py`：轻量上下文存储
- `worker.py`：后台异步任务管理
