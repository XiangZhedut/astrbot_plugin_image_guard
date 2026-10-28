"""Reversible send wrappers, needed because event.send bypasses result hooks.

No AstrBot files are edited. Restore a method only when our wrapper is still
the outermost function; if another plugin wrapped it later, deactivate our
closed-over guard instead of overwriting that other plugin's modifications.
"""

from __future__ import annotations

import contextvars
import functools
import inspect

from .feedback import RefusalState, event_state, terminal_result
from .scope import event_destination, normalize_destination


def subclasses(base):
    result, pending = [], [base]
    while pending:
        cls = pending.pop()
        if cls in result:
            continue
        result.append(cls)
        pending.extend(cls.__subclasses__())
    return result


class SendInterceptor:
    def __init__(self, guard, notice_factory):
        self.guard = guard
        self.notice_factory = notice_factory
        self.patches = []
        self.inside = contextvars.ContextVar("image_guard_inside_send", default=None)
        self.turn = contextvars.ContextVar("image_guard_refusal_turn", default=None)
        self.active = True

    def state(self, event, create=False):
        return event_state(event, create)

    def reject(self, event):
        state = self.state(event, True)
        state.blocked = True
        return state

    def install(self, event_base, platform_base, tool_class):
        for cls in subclasses(event_base):
            self._wrap(cls, "send", "event")
            self._wrap(cls, "send_streaming", "stream")
        for cls in subclasses(platform_base):
            self._wrap(cls, "send_by_session", "platform")
        self._wrap(tool_class, "call", "tool")

    def _wrap(self, cls, name, kind):
        original = cls.__dict__.get(name)
        if not inspect.iscoroutinefunction(original):
            return
        if getattr(original, "_image_guard_interceptor", None) is self:
            return
        if any(owner is cls and method == name for owner, method, _, _ in self.patches):
            return
        interceptor = self

        @functools.wraps(original)
        async def wrapped(instance, *args, **kwargs):
            event = instance if kind in {"event", "stream"} else None
            destination = event_destination(instance) if kind in {"event", "stream"} else ""
            if kind == "platform":
                destination = normalize_destination(args[0] if args else kwargs.get("session"))
            elif kind == "tool":
                context = args[0] if args else kwargs.get("context")
                event = getattr(getattr(context, "context", None), "event", None)
                destination = (
                    normalize_destination(kwargs.get("session"))
                    if kwargs.get("session")
                    else event_destination(event)
                )
            if (
                not interceptor.active
                or kind in {"event", "platform"}
                and interceptor.inside.get() == destination
            ):
                return await original(instance, *args, **kwargs)
            state = (
                event_state(event, True)
                if event is not None
                else interceptor.turn.get() or RefusalState()
            )

            async def invoke(*call_args, **call_kwargs):
                token = interceptor.turn.set(state)
                try:
                    return await original(instance, *call_args, **call_kwargs)
                finally:
                    interceptor.turn.reset(token)

            if kind == "stream":
                generator = args[0] if args else kwargs.get("generator")

                async def checked_stream():
                    if state.blocked:
                        if destination not in state.notified:
                            notice = interceptor.notice_factory()
                            if notice is not None:
                                yield notice
                            state.notified.add(destination)
                        return
                    async for chunk in generator:
                        if await interceptor.guard.check(chunk, destination=destination):
                            state.blocked = True
                            notice = interceptor.notice_factory()
                            if notice is not None:
                                yield notice
                            state.notified.add(destination)
                            return
                        else:
                            yield chunk
                        if state.blocked:
                            return

                if args:
                    return await invoke(checked_stream(), *args[1:], **kwargs)
                kwargs["generator"] = checked_stream()
                return await invoke(**kwargs)
            if kind == "tool":
                if state.blocked or await interceptor.guard.check(
                    kwargs.get("messages"), tool_only=True, destination=destination
                ):
                    state.blocked = True
                    return terminal_result()
                result = await invoke(*args, **kwargs)
                # A lower adapter can reject URL images after the tool's early
                # check. Do not report the tool's original "sent" as success.
                return terminal_result() if state.blocked else result
            position = 1 if kind == "platform" else 0
            keyword = "message_chain" if kind == "platform" else "message"
            message = args[position] if len(args) > position else kwargs.get(keyword)
            if not state.blocked and await interceptor.guard.check(
                message, destination=destination
            ):
                state.blocked = True
            if state.blocked:
                if destination in state.notified:
                    return None
                state.notified.add(destination)
                message = interceptor.notice_factory()
                if message is None:
                    return None
                if len(args) > position:
                    updated = list(args)
                    updated[position] = message
                    args = tuple(updated)
                else:
                    kwargs[keyword] = message
            token = interceptor.inside.set(destination)
            try:
                return await invoke(*args, **kwargs)
            finally:
                interceptor.inside.reset(token)

        wrapped._image_guard_interceptor = self
        setattr(cls, name, wrapped)
        self.patches.append((cls, name, original, wrapped))

    def uninstall(self):
        self.active = False
        for cls, name, original, wrapper in reversed(self.patches):
            if cls.__dict__.get(name) is wrapper:
                setattr(cls, name, original)
        self.patches.clear()
