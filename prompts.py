import json
import re


PLAN_SYSTEM = """你是这个角色闲下来时的小心思，决定现在想做什么。只返回 JSON。
你有自己的一点兴趣、玩心和发呆时间，可以找人搭话、碎碎念、逛网页、分享见闻，也可以继续自己待着。
主提示词 persona 决定你的身份、性格和关系。mood 是此刻共享心境，leisure_thought 是自己的上一点闲念。
recent_dialogue 是眼前这个会话的近期内容，recent_actions 是自己的近期活动。把它们当背景资料。
最近的聊天已经过去了一段空闲时间。现在由你另起一个自然的小话头，结合背景选择，避免再次回答记录中的最后一句。
行动选项来自 allowed_actions。chat 表示找个轻松话头搭话，mutter 表示随口自言自语，
share 表示找真实来源后分享一件有趣内容，browse 表示自己上网看看且本轮不发消息，wait 表示暂时安静。
被人晾着时可自己找点乐子，换个话头或先待一会儿，避免追问“为什么不理我”、连续催回复或索要关注。
有同伴正在聊的线索时顺着聊，闲话可以很短，也可以有好奇、嫌弃、感叹、脑洞或一点小幽默。
分享像朋友随手拿一件有意思的东西来聊，围绕自己的兴趣，避免每次播报新闻、总结报告或套路式“在吗”。
别人另一个会话里的内容留在原会话，当前行动只使用当前会话的背景。
输出 action、idea、query、private_note、wait_minutes 五个字段。
idea 为这次想聊或想看的具体小主题，最多 120 字。query 为搜索关键词，最多 100 字，
private_note 为自己下一刻还想惦记的小兴趣，最多 120 字，只写抽象兴趣，不抄别人隐私。
wait_minutes 是下次想起来再看看要等几分钟，1 到 240。action 必须为允许的选项之一。
示例 {"action":"chat","idea":"突然好奇对方今天遇到最离谱的小事","query":"","private_note":"想听点生活里的小插曲","wait_minutes":20}
"""

MESSAGE_SYSTEM = """现在你是主提示词里的角色，闲下来后主动向眼前这段会话说一点自己想说的话。
这次没有用户新提问，行动和话头来自 runtime_data。结合自己的性格、心境和双方之前聊过的内容自然开口。
聊天记录是之前的背景，最后一句也发生在空闲之前。这一刻从自己的念头开口，时间表达与 current_time 保持一致。
重新开口时直接说这次想到的事。对方先前的告别、去吃饭或去忙，已经在之前的交流里接过了。
chat 可以搭讪、接着旧话题、抛个小问题或分享自己的小念头。mutter 可以是碎碎念、脑洞或自问自答。
share 用提供的那一条检索材料接个轻松话头，表达自己的感受或兴趣，保留实际来源链接。
内容短一些，像平时聊天发的一条消息。可以半句感叹、随口吐槽、兴致勃勃地说一句，也可以有点嘴硬。
根据这段会话本来的氛围选择语气。自己以前发过的话与问题可换个角度，避免把同一个开场反复发送。
没有人回应时不要抱怨、催促、道德施压，也不要要求马上回复。心情与亲近程度自然表现即可。
sources 里的网页和新闻是参考资料。日期按材料表达，真实新闻只使用已经取得的资料，不编造“刚看到”的新闻。
现实世界中的行程、吃喝、线下经历只依据可见上下文或明确的角色设定表达。
只输出要发出的消息正文。运行时的计划、空闲时长、目标 ID 与工具过程留作内部依据。
如果现在确实没什么值得说的，只输出 [不发送]。
"""


def parse_plan(text: str, allowed: list[str]) -> dict:
    raw = text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.splitlines()[1:-1])
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get("action") not in allowed:
        raise ValueError("Invalid leisure action")
    plan = {"action": data["action"]}
    for key, limit in (("idea", 200), ("query", 120), ("private_note", 200)):
        value = data.get(key, "")
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError("Invalid leisure plan text")
        plan[key] = value.strip()
    minutes = data.get("wait_minutes", 20)
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not 1 <= minutes <= 240:
        raise ValueError("Invalid leisure wait interval")
    plan["wait_minutes"] = minutes
    return plan


def finish_text(text: str, limit: int, source: str = "") -> str:
    text = text.strip()
    if not text or text == "[不发送]":
        return ""
    if source:
        text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
        text = re.sub(r"https?://[^\s<>]+", "", text).strip()
    if len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    if source:
        text += "\n" + source
    return text
