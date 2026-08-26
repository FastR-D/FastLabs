"""Shared Feishu onboarding and command documentation."""


SETUP_STEPS = [
    {
        "title": "1. 创建应用",
        "body": "在飞书开放平台创建企业自建应用，并启用机器人能力。",
    },
    {
        "title": "2. 申请权限",
        "body": (
            "开通 im:message.p2p_msg:readonly、im:message.group_at_msg:readonly "
            "和 im:message:send_as_bot。"
        ),
    },
    {
        "title": "3. 配置长连接",
        "body": (
            "在事件与回调中选择长连接，订阅 im.message.receive_v1，"
            "并添加 card.action.trigger 卡片回调。"
        ),
    },
    {
        "title": "4. 发布并配置",
        "body": (
            "发布应用版本，取得 App ID、App Secret 和允许用户的 Open ID，"
            "写入 .fastlab/fastlab.env 后重启 FastLab。"
        ),
    },
    {
        "title": "5. 验证",
        "body": "设置页应显示已连接；私聊发送“帮助”，群聊需要先 @ 机器人。",
    },
]


COMMANDS = [
    {
        "name": "帮助",
        "purpose": "查看可用命令",
        "usage": "帮助 [详细]",
        "details": "发送“帮助”查看速查；发送“帮助 详细”查看完整卡片。",
        "example": "帮助 详细",
    },
    {
        "name": "仓库",
        "purpose": "查看可以工作的仓库别名",
        "usage": "仓库",
        "details": "只显示别名、默认标记和可用状态，不显示电脑上的绝对路径。",
        "example": "仓库",
    },
    {
        "name": "执行器",
        "purpose": "查看当前可用的 Codex 和 Claude",
        "usage": "执行器",
        "details": "FastLab 只支持这两种固定执行器，不需要额外配置执行类型。",
        "example": "执行器",
    },
    {
        "name": "创建",
        "purpose": "在指定仓库生成只读执行计划",
        "usage": "创建 <仓库别名> <目标>，或使用多行格式",
        "details": "仓库别名和目标必填；目录、标题、限制可选；并发为 1–32。FastLab 使用固定 orchestration Skill 拆分任务并分配执行器。",
        "example": (
            "创建 fastlab\n目录：web\n标题：优化移动端\n"
            "目标：改进任务详情页的窄屏体验\n限制：保持桌面端行为不变\n并发：2"
        ),
    },
    {
        "name": "状态",
        "purpose": "查询任务状态、进度和错误",
        "usage": "状态 <任务ID>",
        "details": "任务 ID 至少使用 4 位唯一前缀，建议复制卡片上的 8 位 ID。",
        "example": "状态 A1B2C3D4",
    },
    {
        "name": "分配",
        "purpose": "调整 Skill 为子任务选择的执行器",
        "usage": "分配 <任务ID> <子任务> <Codex|Claude> [| 模型 | 推理等级]",
        "details": "只适用于等待确认的任务；计划已自动分配，只有需要时才调整。",
        "example": "分配 A1B2C3D4 S1 Claude | sonnet | high",
    },
    {
        "name": "验收",
        "purpose": "指定最终只读检查使用的执行器",
        "usage": "验收 <任务ID> <Codex|Claude> [| 模型 | 推理等级]",
        "details": "验收只读检查集成分支；必须在确认执行前指定。",
        "example": "验收 A1B2C3D4 Codex | gpt-5 | high",
    },
    {
        "name": "调整",
        "purpose": "在执行前根据反馈重新规划",
        "usage": "调整 <任务ID> <反馈>",
        "details": "只适用于尚未执行的等待确认、规划失败或需要处理任务。",
        "example": "调整 A1B2C3D4 请减少子任务数量",
    },
    {
        "name": "确认",
        "purpose": "确认计划并开始执行",
        "usage": "确认 <任务ID>",
        "details": "仅等待确认状态可用；开始前要求目标仓库干净。",
        "example": "确认 A1B2C3D4",
    },
    {
        "name": "取消",
        "purpose": "停止正在规划、执行或验收的任务",
        "usage": "取消 <任务ID>",
        "details": "已完成或已经停止的任务不能再次取消。",
        "example": "取消 A1B2C3D4",
    },
    {
        "name": "追加",
        "purpose": "复用仍保留的原子任务 Agent 会话",
        "usage": "追加 <任务ID> <子任务ID> <说明>",
        "details": "只续跑指定子任务；必须仍保留 Worktree 和会话。现场清理后请使用“继续”。",
        "example": "追加 A1B2C3D4 S2 再覆盖 Windows 路径",
    },
    {
        "name": "继续",
        "purpose": "从已交付代码创建新的修改任务",
        "usage": "继续 <任务ID> <追加要求>",
        "details": "只适用于已成功交付的任务；生成新计划和新 Worktree，不恢复旧 Agent 会话。",
        "example": "继续 A1B2C3D4 请增加导出 CSV 功能",
    },
    {
        "name": "重试",
        "purpose": "重新执行失败、阻塞或取消的子任务",
        "usage": "重试 <任务ID> <子任务ID>",
        "details": "会保留该子任务原有 Worktree，便于修正失败现场。",
        "example": "重试 A1B2C3D4 S2",
    },
]


def help_payload():
    return {
        "setup": [dict(item) for item in SETUP_STEPS],
        "commands": [dict(item) for item in COMMANDS],
        "configExample": (
            "# .fastlab/fastlab.env\n"
            "FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx\n"
            "FEISHU_APP_SECRET=请填入应用密钥\n"
            "FASTLAB_FEISHU_ALLOWED_OPEN_IDS=ou_xxxxxxxxxxxxxxxx"
        ),
        "notes": [
            "普通聊天不会自动创建任务。",
            "私聊只接受白名单用户；群聊必须 @ 机器人。",
            "确认计划前不会创建 Worktree 或修改目标仓库。",
        ],
    }


def concise_help():
    rows = ["FastLab 命令速查（发送“帮助 详细”查看完整说明）"]
    rows.extend("• %s：%s\n  %s" % (item["name"], item["purpose"], item["usage"])
                for item in COMMANDS)
    rows.append("普通聊天不会自动交给执行器。")
    return "\n".join(rows)


def detailed_help_card():
    elements = [{
        "tag": "markdown",
        "content": (
            "FastLab 先生成只读计划，只有确认后才执行。任务必须选择已登记仓库；"
            "`目录` 是仓库内可选的相对工作范围。"
        ),
    }]
    groups = [
        ("仓库与创建", {"仓库", "创建"}),
        ("任务分配", {"执行器", "分配", "验收"}),
        ("查看与调整", {"状态", "调整", "追加", "继续"}),
        ("执行控制", {"确认", "取消", "重试"}),
        ("帮助", {"帮助"}),
    ]
    for title, names in groups:
        elements.append({"tag": "markdown", "content": "## %s" % title})
        for item in COMMANDS:
            if item["name"] not in names:
                continue
            elements.append({
                "tag": "markdown",
                "content": "**%s · %s**\n格式：`%s`\n%s\n示例：\n```text\n%s\n```" % (
                    item["name"], item["purpose"], item["usage"], item["details"], item["example"]
                ),
            })
    elements.append({
        "tag": "markdown",
        "content": (
            "## 状态与安全边界\n"
            "计划会自动分配每个子任务，用户选择最终验收执行器后即可确认；"
            "调整只适用于执行前；追加复用仍保留的原子任务会话；"
            "继续会从已交付代码创建新任务；"
            "重试只适用于失败、阻塞或取消的子任务。私聊仅限白名单，群聊必须 @ 机器人，"
            "目录外改动不会提交或合并。\n\n"
            "## 常见错误\n"
            "仓库不可用时请回电脑检查登记路径；任务 ID 至少 4 位且必须唯一；"
            "目录必须是仓库内已存在的相对文件夹；普通聊天不会交给执行器。"
        ),
    })
    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "FastLab · 完整命令帮助"},
            "template": "blue",
        },
        "body": {"elements": elements},
    }
