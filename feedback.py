"""Terminal image refusal state, scoped to one incoming event."""

import json
from dataclasses import dataclass, field

DEFAULT_NOTICE = "这张图片受禁发或会话隔离规则保护，不能发送。本次请求已终止，请勿重复尝试或通过重新上传、改格式、换群等方式重试。"
RESPONSE_POLICY = "[ImageGuard 图片发送约束] 图片被 ImageGuard 拦截是终止结果，不是可重试的发送故障。不得建议用户重发、重新上传、换格式、改路径、换群、找他人转发或通过其他工具重试。不得谎称已发送。只简短说明图片不能发送、本次请求已终止，然后可以继续无关话题。"


@dataclass
class RefusalState:
    blocked: bool = False
    notified: set = field(default_factory=set)


def event_state(event, create=False):
    state = getattr(event, "_image_guard_refusal", None) if event is not None else None
    if state is None and create:
        state = RefusalState()
        if event is not None:
            setattr(event, "_image_guard_refusal", state)
    return state


def terminal_result():
    return "error: ImageGuard blocked this image. " + json.dumps(
        {
            "status": "blocked",
            "retryable": False,
            "request_terminated": True,
            "user_message": DEFAULT_NOTICE,
            "instruction": RESPONSE_POLICY,
        },
        ensure_ascii=False,
    )
