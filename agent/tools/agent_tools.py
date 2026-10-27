import os.path

import requests
from langchain_core.tools import tool
import random
from utils.config_handler import agent_conf
from utils.logger_handler import logger
from utils.path_tool import get_abs_path

user_ids  = ["1001", "1002", "1003", "1004", "1005", "1006", "1007", "1008", "1009", "1010"]
month_arr = ["2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06", "2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12"]
external_data = {}

@tool(description="获取指定城市天气，以消息字符串的形式返回")
def get_weather(city: str) -> str:
    return f"城市{city}天气为晴天，气温26摄氏度，空气湿度50%，南风1级，AQI21，最近6小时降雨概率极低"


@tool(description="获取用户所在城市的名称，以纯字符串形式返回")
def get_user_location() -> str:
    return random.choice(["深圳","合肥","杭州"])


@tool(description="获取用户ID，以纯字符串形式返回")
def get_user_id() -> str:
    return random.choice(user_ids)


@tool(description="获取当前月份，以纯字符串形式返回")
def get_current_month() -> str:
    return random.choice(month_arr)


def generate_external_data():
    """
    {
        "user_id": {
            "month" : {"特征": xxx, "效率": xxx, ...}
            "month" : {"特征": xxx, "效率": xxx, ...}
            "month" : {"特征": xxx, "效率": xxx, ...}
            ...
        },
        "user_id": {
            "month" : {"特征": xxx, "效率": xxx, ...}
            "month" : {"特征": xxx, "效率": xxx, ...}
            "month" : {"特征": xxx, "效率": xxx, ...}
            ...
        },
        "user_id": {
            "month" : {"特征": xxx, "效率": xxx, ...}
            "month" : {"特征": xxx, "效率": xxx, ...}
            "month" : {"特征": xxx, "效率": xxx, ...}
            ...
        },
        ...
    }
    :return:
    """
    if not external_data:
        external_data_path = get_abs_path(agent_conf["external_data_path"])

        if not os.path.exists(external_data_path):
            raise FileNotFoundError(f"外部数据文件{external_data_path}不存在")

        with open(external_data_path, "r", encoding="utf-8") as f:
            for line in f.readlines()[1:]:
                arr: list[str] = line.strip().split(",")

                user_id: str = arr[0].replace('"',"")
                feature: str = arr[1].replace('"',"")
                efficiency: str = arr[2].replace('"',"")
                consumables: str = arr[3].replace('"',"")
                comparison: str = arr[4].replace('"',"")
                time: str = arr[5].replace('"',"")

                if user_id not in external_data:
                    external_data[user_id] = {}

                external_data[user_id][time] = {
                    "特征": feature,
                    "效率": efficiency,
                    "消耗": consumables,
                    "比较": comparison
                }

@tool(description="从外部系统中获取指定用户在指定月份的使用记录，以纯字符串形式返回，如果未检索到则返回空字符串")
def fetch_external_data(user_id: str,month:str)-> str:
    generate_external_data()

    try:
        return external_data[user_id][month]
    except KeyError:
        logger.warning(f"[fetch_external_data]未能检索到用户：{user_id}在{month}的使用记录数据")
        return ""


@tool(description="无入参，无返回值，调用后触发中间件自动为报告生成的场景动态注入上下文信息，为后续提示词切换提供上下文信息")
def fill_context_for_report():
    return "fill_context_for_report已调用"


@tool(description="调用百度联网搜索实时信息，输入字符串查询内容，以纯字符串形式返回搜索结果；未配置密钥或网络/鉴权失败时抛出异常，由中间件转成可重试的错误消息")
def web_search_baidu(query: str, **kwargs) -> str:
    api_key_baidu = os.getenv("API_KEY_BAIDU")
    baidu_websearch_url = "https://qianfan.baidubce.com/v2/ai_search/chat/completions"

    if not api_key_baidu:
        # 配置问题属于确定性失败：直接抛，不重试，让模型明确告诉用户去补 .env
        raise RuntimeError(
            "未配置环境变量 API_KEY_BAIDU，联网搜索不可用；请在 .env 中填写后重启应用。"
        )

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key_baidu}"
    }
    payload = {
        "messages": [{"role": "user", "content": query}],
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": 5},
                                 {"type": "image", "top_k": 3},
                                 {"type": "video", "top_k": 2}],
    }

    try:
        # timeout=(连接超时, 读取超时)：避免一个慢请求把整轮对话卡住
        response = requests.post(
            baidu_websearch_url, headers=headers, json=payload, timeout=(5, 30)
        )
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as exc:
        # 不吞异常：重试与“转成错误消息”交给 ToolRetryMiddleware / ToolErrorMiddleware，
        # 模型因此能看到真实原因（401、超时、5xx），而不是收到一个空字符串。
        logger.warning(f"[web_search_baidu]调用百度AI搜索失败：{exc}")
        raise

    def _pick_text(*vals) -> str:
        """按优先级从若干候选字段中挑出第一个非空字符串。"""
        for v in vals:
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    def _truncate(text: str, limit: int = 600) -> str:
        """截断过长摘要，避免单条 reference 挤占上下文。"""
        if not text:
            return ""
        return text if len(text) <= limit else text[:limit] + "…"

    parts = []
    ref_blocks = []
    for ref in data.get("references") or []:
        rtype = (ref.get("type") or "").lower()
        title = _pick_text(ref.get("title"))
        url = _pick_text(ref.get("url"))
        # 摘要优先级：正文 content > 简介 snippet > markdown_content
        summary = _truncate(_pick_text(
            ref.get("content"), ref.get("snippet"), ref.get("markdown_content")
        ))
        source = _pick_text(ref.get("website"), ref.get("web_anchor"))
        date = _pick_text(ref.get("date"))

        # 完全没有标题/摘要/链接的脏数据，丢弃
        if not (title or summary or url):
            continue

        # 类型标签：区分 image/video/web，并标注权威来源
        kind_tag = rtype or "unknown"
        if ref.get("is_aladdin"):
            kind_tag += "|权威百科"
        elif ref.get("authority_score"):
            kind_tag += f"|权威度{ref['authority_score']}"

        lines = [f"[{ref.get('id', '')}] {title or '(无标题)'}",
                 f"- 类型：{kind_tag}"]

        # 图片专用：输出图片直链 + 尺寸，其余字段能省则省
        if rtype == "image":
            img = ref.get("image") or {}
            img_url = _pick_text(img.get("url"))
            w, h = img.get("width"), img.get("height")
            if img_url:
                lines.append(f"- 图片：{img_url}")
            if w and h:
                lines.append(f"- 尺寸：{w}x{h}")
            if date:
                lines.append(f"- 时间：{date}")
            if url:
                lines.append(f"- 链接：{url}")
            ref_blocks.append("\n".join(lines))
            continue

        if source:
            lines.append(f"- 来源：{source}")
        if date:
            lines.append(f"- 时间：{date}")
        if rtype == "video":
            duration = (ref.get("video") or {}).get("duration")
            if duration:
                lines.append(f"- 时长：{duration}s")
        if summary:
            lines.append(f"- 摘要：{summary}")
        if url:
            lines.append(f"- 链接：{url}")
        ref_blocks.append("\n".join(lines))

    if ref_blocks:
        parts.append("【参考文献】\n" + "\n\n".join(ref_blocks))

    if not parts:
        logger.warning(f"[web_search_baidu]未检索到与{query}相关的结果")
        # 搜索成功但没有结果，是“查不到”而不是“失败”：给一句可读的说明，
        # 避免模型把空字符串误当成静默成功。
        return f"（百度搜索没有返回与「{query}」相关的可用结果）"

    return "\n\n".join(parts)


if __name__ == '__main__':
    print(web_search_baidu("哈基米是什么梗"))